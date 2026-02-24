"""Endpoints de factura POS: creación/submit/cancelación e impresión."""

import base64
from typing import Any

import frappe
from frappe.translate import print_language
from frappe.utils.data import nowdate
from frappe.utils.print_utils import get_print

from .common import (
	complete_idempotency,
	get_idempotency_result,
	ok,
	parse_payload,
	payload_hash,
	resolve_client_request_id,
	standard_api_response,
	to_bool,
	value_from_aliases,
)
from .settings import enforce_api_access, enforce_doctype_permission


_INTERNAL_MUTATION_KEYS = {"client_request_id", "clientRequestId", "request_id", "requestId", "payload", "cmd"}
_PRINT_RESPONSE_MODES = {"base64", "file_url", "both"}
_PDF_GENERATORS = {"wkhtmltopdf", "chrome"}


def _coerce_float(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except Exception:
		return default


def _resolve_print_options(
	doctype: str,
	requested_print_format: str | None,
) -> tuple[str, str, list[str]]:
	meta = frappe.get_meta(doctype)
	default_print_format = str(meta.default_print_format or "Standard").strip() or "Standard"

	filters: dict[str, Any] = {"doc_type": doctype, "print_format_for": "DocType", "disabled": 0}
	available_print_formats = frappe.get_all(
		"Print Format",
		filters=filters,
		pluck="name",
		page_length=0,
		order_by="name asc",
	)
	if "Standard" not in available_print_formats:
		available_print_formats = ["Standard", *available_print_formats]

	selected_print_format = (requested_print_format or "").strip() or default_print_format
	if selected_print_format not in available_print_formats:
		frappe.throw(f"Print Format {selected_print_format} is not configured for {doctype}")

	return selected_print_format, default_print_format, available_print_formats


def _print_kwargs_from_payload(body: dict[str, Any]) -> dict[str, Any]:
	no_letterhead = 1 if to_bool(value_from_aliases(body, "no_letterhead", "noLetterhead"), default=False) else 0
	letterhead = value_from_aliases(body, "letterhead")
	language = value_from_aliases(body, "language", "lang")
	return {
		"no_letterhead": no_letterhead,
		"letterhead": letterhead,
		"lang": language,
	}


def _resolve_pdf_generator(body: dict[str, Any], print_format: str) -> str:
	requested = str(value_from_aliases(body, "pdf_generator", "pdfGenerator", default="") or "").strip().lower()
	if requested:
		if requested not in _PDF_GENERATORS:
			frappe.throw(f"pdf_generator must be one of: {', '.join(sorted(_PDF_GENERATORS))}")
		return requested

	configured = str(frappe.get_cached_value("Print Format", print_format, "pdf_generator") or "").strip().lower()
	if configured in _PDF_GENERATORS:
		return configured
	return "wkhtmltopdf"


def _is_missing_wkhtmltopdf_error(exc: Exception) -> bool:
	message = str(exc or "").lower()
	if "wkhtmltopdf" not in message:
		return False
	return any(token in message for token in ("no wkhtmltopdf", "executable", "not found", "no such file"))


def _generate_invoice_pdf_bytes(
	*,
	name: str,
	doc,
	print_format: str,
	print_kwargs: dict[str, Any],
	pdf_generator: str,
) -> bytes:
	form_dict = getattr(frappe.local, "form_dict", None)
	if form_dict is None:
		form_dict = frappe._dict()
		frappe.local.form_dict = form_dict
	had_pdf_generator = "pdf_generator" in form_dict
	previous_pdf_generator = form_dict.get("pdf_generator")
	form_dict["pdf_generator"] = pdf_generator
	try:
		with print_language(print_kwargs["lang"] or frappe.local.lang):
			pdf_value = get_print(
				doctype="Sales Invoice",
				name=name,
				print_format=print_format,
				doc=doc,
				as_pdf=True,
				no_letterhead=print_kwargs["no_letterhead"],
				letterhead=print_kwargs["letterhead"],
				pdf_generator=pdf_generator,  # type: ignore[arg-type]
			)
	finally:
		if had_pdf_generator:
			form_dict["pdf_generator"] = previous_pdf_generator
		else:
			form_dict.pop("pdf_generator", None)
	if not isinstance(pdf_value, (bytes, bytearray)):
		frappe.throw("Unable to generate PDF bytes")
	return bytes(pdf_value)


def _safe_invoice_filename(invoice_name: str, print_format: str, extension: str) -> str:
	base = f"{invoice_name}-{print_format}".replace("/", "-").replace("\\", "-").replace(" ", "_")
	return f"{base}.{extension}"


def _create_file_for_pdf(
	*,
	invoice_name: str,
	print_format: str,
	pdf_bytes: bytes,
	is_private: bool,
) -> dict[str, Any]:
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": _safe_invoice_filename(invoice_name, print_format, "pdf"),
			"attached_to_doctype": "Sales Invoice",
			"attached_to_name": invoice_name,
			"is_private": 1 if is_private else 0,
			"content": pdf_bytes,
		}
	)
	file_doc.insert(ignore_permissions=True)
	return {
		"name": file_doc.name,
		"file_url": file_doc.file_url,
		"is_private": int(file_doc.is_private or 0),
	}


def _normalize_invoice_items(
	value: Any,
	*,
	default_warehouse: str | None,
) -> list[dict[str, Any]]:
	rows = value if isinstance(value, list) else []
	items: list[dict[str, Any]] = []
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		row = dict(raw)
		item_code = str(value_from_aliases(row, "item_code", "itemCode", default="") or "").strip()
		if not item_code:
			continue

		qty = _coerce_float(value_from_aliases(row, "qty", "quantity", default=0), 0.0)
		rate_raw = value_from_aliases(row, "rate", "price", default=None)
		amount_raw = value_from_aliases(row, "amount", default=None)
		rate = _coerce_float(rate_raw, 0.0) if rate_raw is not None else None
		amount = _coerce_float(amount_raw, 0.0) if amount_raw is not None else None
		if amount is None and rate is not None:
			amount = qty * rate

		row["item_code"] = item_code
		row["qty"] = qty
		if rate is not None:
			row["rate"] = rate
		if amount is not None:
			row["amount"] = amount
		if not row.get("warehouse") and default_warehouse:
			row["warehouse"] = default_warehouse
		items.append(row)
	return items


def _normalize_invoice_payments(value: Any) -> list[dict[str, Any]]:
	rows = value if isinstance(value, list) else []
	payments: list[dict[str, Any]] = []
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		row = dict(raw)
		mode_of_payment = str(value_from_aliases(row, "mode_of_payment", "modeOfPayment", default="") or "").strip()
		if not mode_of_payment:
			continue
		amount = _coerce_float(value_from_aliases(row, "amount", default=0), 0.0)
		row["mode_of_payment"] = mode_of_payment
		row["amount"] = amount
		if not row.get("type"):
			row["type"] = "Receive"
		payments.append(row)
	return payments


def _normalize_create_payload(body: dict[str, Any]) -> dict[str, Any]:
	doc_payload = {k: v for k, v in body.items() if k not in _INTERNAL_MUTATION_KEYS}
	default_warehouse = str(value_from_aliases(body, "set_warehouse", "setWarehouse", default="") or "").strip()

	alias_map = {
		"customer": value_from_aliases(body, "customer", "customer_id", "customerId"),
		"customer_name": value_from_aliases(body, "customer_name", "customerName"),
		"company": value_from_aliases(body, "company"),
		"posting_date": value_from_aliases(body, "posting_date", "postingDate", default=nowdate()),
		"posting_time": value_from_aliases(body, "posting_time", "postingTime"),
		"due_date": value_from_aliases(body, "due_date", "dueDate"),
		"territory": value_from_aliases(body, "territory"),
		"is_pos": value_from_aliases(body, "is_pos", "isPos"),
		"update_stock": value_from_aliases(body, "update_stock", "updateStock"),
		"set_warehouse": value_from_aliases(body, "set_warehouse", "setWarehouse"),
		"selling_price_list": value_from_aliases(body, "selling_price_list", "sellingPriceList", "price_list", "priceList"),
		"currency": value_from_aliases(body, "currency"),
		"conversion_rate": value_from_aliases(body, "conversion_rate", "conversionRate"),
		"naming_series": value_from_aliases(body, "naming_series", "namingSeries"),
		"disable_rounded_total": value_from_aliases(body, "disable_rounded_total", "disableRoundedTotal"),
		"rounded_total": value_from_aliases(body, "rounded_total", "roundedTotal"),
		"total_taxes_and_charges": value_from_aliases(body, "total_taxes_and_charges", "totalTaxesAndCharges"),
		"grand_total": value_from_aliases(body, "grand_total", "grandTotal"),
		"pos_profile": value_from_aliases(body, "pos_profile", "posProfile"),
		"pos_opening_entry": value_from_aliases(body, "pos_opening_entry", "posOpeningEntry"),
		"is_return": value_from_aliases(body, "is_return", "isReturn"),
		"return_against": value_from_aliases(body, "return_against", "returnAgainst"),
		"party_account_currency": value_from_aliases(body, "party_account_currency", "partyAccountCurrency"),
		"custom_payment_currency": value_from_aliases(body, "custom_payment_currency", "customPaymentCurrency"),
		"custom_exchange_rate": value_from_aliases(body, "custom_exchange_rate", "customExchangeRate"),
		"posa_delivery_charges": value_from_aliases(body, "posa_delivery_charges", "posaDeliveryCharges"),
	}
	for key, value in alias_map.items():
		if value is None:
			continue
		doc_payload[key] = value

	doc_payload["items"] = _normalize_invoice_items(
		value_from_aliases(body, "items", "invoice_items", "invoiceItems", default=[]),
		default_warehouse=default_warehouse or None,
	)
	doc_payload["payments"] = _normalize_invoice_payments(value_from_aliases(body, "payments", default=[]))
	doc_payload.pop("doctype", None)
	doc_payload.pop("docstatus", None)
	return doc_payload


def _validate_create_payload(doc_payload: dict[str, Any]) -> None:
	company = str(doc_payload.get("company") or "").strip()
	customer = str(doc_payload.get("customer") or "").strip()
	items = doc_payload.get("items") if isinstance(doc_payload.get("items"), list) else []
	is_return = to_bool(doc_payload.get("is_return"), default=False)

	if not company:
		frappe.throw("company is required")
	if not customer:
		frappe.throw("customer is required")
	if not items:
		frappe.throw("items are required")

	for idx, item in enumerate(items, start=1):
		item_code = str(item.get("item_code") or "").strip()
		qty = _coerce_float(item.get("qty"), 0.0)
		if not item_code:
			frappe.throw(f"items[{idx}].item_code is required")
		if qty == 0:
			frappe.throw(f"items[{idx}].qty cannot be 0")
		if qty < 0 and not is_return:
			frappe.throw(f"items[{idx}].qty cannot be negative on non-return invoice")


@frappe.whitelist(methods=["POST"])
@standard_api_response
def create_submit(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
	enforce_api_access()
	enforce_doctype_permission("Sales Invoice", "create")
	enforce_doctype_permission("Sales Invoice", "submit")
	body = parse_payload(payload)
	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "sales_invoice.create_submit"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc_payload = _normalize_create_payload(body)
	_validate_create_payload(doc_payload)
	doc_payload["doctype"] = "Sales Invoice"
	doc = frappe.get_doc(doc_payload)
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()
	result = {
		"name": doc.name,
		"docstatus": int(doc.docstatus or 0),
		"status": doc.get("status"),
		"company": doc.get("company"),
		"customer": doc.get("customer"),
		"customer_name": doc.get("customer_name"),
		"posting_date": str(doc.get("posting_date")) if doc.get("posting_date") else None,
		"grand_total": _coerce_float(doc.get("grand_total"), 0.0),
		"outstanding_amount": _coerce_float(doc.get("outstanding_amount"), 0.0),
		"modified": str(doc.get("modified")) if doc.get("modified") else None,
		"items_count": len(doc.get("items") or []),
		"payments_count": len(doc.get("payments") or []),
	}

	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype="Sales Invoice",
		reference_name=doc.name,
	)
	return ok(result, request_id=request_id)


@frappe.whitelist(methods=["POST"])
@standard_api_response
def cancel(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	name = (body.get("name") or "").strip()
	if not name:
		frappe.throw("name is required")

	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "sales_invoice.cancel"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc = frappe.get_doc("Sales Invoice", name)
	enforce_doctype_permission("Sales Invoice", "cancel", doc=doc)
	doc.flags.ignore_permissions = True
	doc.cancel()
	result = {"name": doc.name, "docstatus": doc.docstatus}

	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype="Sales Invoice",
		reference_name=doc.name,
	)
	return ok(result, request_id=request_id)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def print_options(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	name = str(value_from_aliases(body, "name", "sales_invoice", "invoice_name", "invoiceName", default="") or "").strip()
	print_format = str(value_from_aliases(body, "print_format", "printFormat", default="") or "").strip() or None

	if name:
		doc = frappe.get_doc("Sales Invoice", name)
		enforce_doctype_permission("Sales Invoice", "print", doc=doc)
	else:
		enforce_doctype_permission("Sales Invoice", "print")

	selected_print_format, default_print_format, available_print_formats = _resolve_print_options(
		"Sales Invoice", print_format
	)
	data = {
		"name": name or None,
		"doctype": "Sales Invoice",
		"default_print_format": default_print_format,
		"selected_print_format": selected_print_format,
		"available_print_formats": available_print_formats,
	}
	return ok(data)


@frappe.whitelist(methods=["POST"])
@standard_api_response
def print_html(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	name = str(value_from_aliases(body, "name", "sales_invoice", "invoice_name", "invoiceName", default="") or "").strip()
	if not name:
		frappe.throw("name is required")

	doc = frappe.get_doc("Sales Invoice", name)
	enforce_doctype_permission("Sales Invoice", "print", doc=doc)
	requested_print_format = str(value_from_aliases(body, "print_format", "printFormat", default="") or "").strip() or None
	selected_print_format, default_print_format, available_print_formats = _resolve_print_options(
		"Sales Invoice", requested_print_format
	)
	print_kwargs = _print_kwargs_from_payload(body)
	with print_language(print_kwargs["lang"] or frappe.local.lang):
		html = get_print(
			doctype="Sales Invoice",
			name=name,
			print_format=selected_print_format,
			doc=doc,
			as_pdf=False,
			no_letterhead=print_kwargs["no_letterhead"],
			letterhead=print_kwargs["letterhead"],
		)
	data = {
		"name": name,
		"doctype": "Sales Invoice",
		"default_print_format": default_print_format,
		"print_format": selected_print_format,
		"available_print_formats": available_print_formats,
		"content_type": "text/html",
		"filename": _safe_invoice_filename(name, selected_print_format, "html"),
		"html": html,
	}
	return ok(data)


@frappe.whitelist(methods=["POST"])
@standard_api_response
def print_pdf(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	name = str(value_from_aliases(body, "name", "sales_invoice", "invoice_name", "invoiceName", default="") or "").strip()
	if not name:
		frappe.throw("name is required")

	doc = frappe.get_doc("Sales Invoice", name)
	enforce_doctype_permission("Sales Invoice", "print", doc=doc)
	requested_print_format = str(value_from_aliases(body, "print_format", "printFormat", default="") or "").strip() or None
	selected_print_format, default_print_format, available_print_formats = _resolve_print_options(
		"Sales Invoice", requested_print_format
	)
	response_mode = str(value_from_aliases(body, "response_mode", "responseMode", default="base64") or "base64").strip().lower()
	if response_mode not in _PRINT_RESPONSE_MODES:
		frappe.throw(f"response_mode must be one of: {', '.join(sorted(_PRINT_RESPONSE_MODES))}")

	print_kwargs = _print_kwargs_from_payload(body)
	resolved_pdf_generator = _resolve_pdf_generator(body, selected_print_format)
	generators_to_try: list[str] = [resolved_pdf_generator]
	if resolved_pdf_generator == "wkhtmltopdf":
		generators_to_try.append("chrome")

	pdf_bytes: bytes | None = None
	pdf_generator_used = resolved_pdf_generator
	last_error: Exception | None = None
	for generator_name in generators_to_try:
		try:
			pdf_bytes = _generate_invoice_pdf_bytes(
				name=name,
				doc=doc,
				print_format=selected_print_format,
				print_kwargs=print_kwargs,
				pdf_generator=generator_name,
			)
			pdf_generator_used = generator_name
			break
		except Exception as exc:  # noqa: BLE001 - we retry only for wkhtmltopdf missing binary.
			last_error = exc
			if generator_name == "wkhtmltopdf" and _is_missing_wkhtmltopdf_error(exc):
				continue
			raise

	if pdf_bytes is None:
		details = f" Details: {last_error}" if last_error else ""
		frappe.throw(
			"Unable to generate PDF. Install wkhtmltopdf or configure PDF generator as chrome in Print Settings."
			+ details
		)

	file_info = None
	if response_mode in {"file_url", "both"}:
		is_private = to_bool(value_from_aliases(body, "is_private", "isPrivate"), default=True)
		file_info = _create_file_for_pdf(
			invoice_name=name,
			print_format=selected_print_format,
			pdf_bytes=pdf_bytes,
			is_private=is_private,
		)

	pdf_base64 = None
	if response_mode in {"base64", "both"}:
		pdf_base64 = base64.b64encode(pdf_bytes).decode("ascii")

	data = {
		"name": name,
		"doctype": "Sales Invoice",
		"default_print_format": default_print_format,
		"print_format": selected_print_format,
		"available_print_formats": available_print_formats,
		"content_type": "application/pdf",
		"filename": _safe_invoice_filename(name, selected_print_format, "pdf"),
		"response_mode": response_mode,
		"pdf_generator": pdf_generator_used,
		"pdf_base64": pdf_base64,
		"file": file_info,
	}
	return ok(data)
