"""Endpoints de Payment Entry POS con normalización de payload e idempotencia."""

from typing import Any

import frappe
from frappe.utils.data import nowdate

from .common import (
	ok,
	parse_payload,
	standard_api_response,
)


_INTERNAL_MUTATION_KEYS = {"client_request_id", "request_id", "payload", "cmd"}


def _coerce_float(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except Exception:
		return default


def _normalize_references(value: Any) -> list[dict[str, Any]]:
	rows = value if isinstance(value, list) else []
	references: list[dict[str, Any]] = []
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		row = dict(raw)
		reference_doctype = str(row.get("reference_doctype") or "Sales Invoice").strip()
		reference_name = str(row.get("reference_name") or "").strip()
		if not reference_name:
			continue
		row["reference_doctype"] = reference_doctype or "Sales Invoice"
		row["reference_name"] = reference_name
		if "allocated_amount" in row:
			row["allocated_amount"] = _coerce_float(row.get("allocated_amount"), 0.0)
		if "outstanding_amount" in row:
			row["outstanding_amount"] = _coerce_float(row.get("outstanding_amount"), 0.0)
		if "total_amount" in row:
			row["total_amount"] = _coerce_float(row.get("total_amount"), 0.0)
		references.append(row)
	return references


def _normalize_create_payload(body: dict[str, Any]) -> dict[str, Any]:
	doc_payload = {k: v for k, v in body.items() if k not in _INTERNAL_MUTATION_KEYS}
	for fieldname in (
		"company",
		"posting_date",
		"payment_type",
		"party_type",
		"party",
		"mode_of_payment",
		"paid_amount",
		"received_amount",
		"paid_from",
		"paid_to",
		"paid_to_account_currency",
		"source_exchange_rate",
		"target_exchange_rate",
		"reference_no",
		"reference_date",
		"received_from",
	):
		value = body.get(fieldname)
		if value is not None:
			doc_payload[fieldname] = value

	doc_payload.setdefault("posting_date", nowdate())
	doc_payload.setdefault("payment_type", "Receive")
	doc_payload.setdefault("party_type", "Customer")

	doc_payload["paid_amount"] = _coerce_float(doc_payload.get("paid_amount"), 0.0)
	doc_payload["received_amount"] = _coerce_float(doc_payload.get("received_amount"), 0.0)
	doc_payload["references"] = _normalize_references(body.get("references"))
	doc_payload.pop("doctype", None)
	doc_payload.pop("docstatus", None)
	return doc_payload


def _validate_create_payload(doc_payload: dict[str, Any]) -> None:
	company = str(doc_payload.get("company") or "").strip()
	party = str(doc_payload.get("party") or "").strip()
	payment_type = str(doc_payload.get("payment_type") or "").strip()
	party_type = str(doc_payload.get("party_type") or "").strip()
	paid_amount = _coerce_float(doc_payload.get("paid_amount"), 0.0)
	received_amount = _coerce_float(doc_payload.get("received_amount"), 0.0)
	references = doc_payload.get("references") if isinstance(doc_payload.get("references"), list) else []

	if not company:
		frappe.throw("company is required")
	if payment_type != "Internal Transfer" and not party:
		frappe.throw("party is required")
	if not payment_type:
		frappe.throw("payment_type is required")
	if payment_type != "Internal Transfer" and not party_type:
		frappe.throw("party_type is required")
	if paid_amount <= 0 and received_amount <= 0:
		frappe.throw("paid_amount or received_amount must be greater than 0")

	for idx, ref in enumerate(references, start=1):
		if not str(ref.get("reference_name") or "").strip():
			frappe.throw(f"references[{idx}].reference_name is required")
		if not str(ref.get("reference_doctype") or "").strip():
			frappe.throw(f"references[{idx}].reference_doctype is required")
		allocated_amount = _coerce_float(ref.get("allocated_amount"), 0.0)
		if allocated_amount <= 0:
			frappe.throw(f"references[{idx}].allocated_amount must be greater than 0")


@frappe.whitelist(methods=["POST"])
@standard_api_response
def create_submit(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)

	doc_payload = _normalize_create_payload(body)
	_validate_create_payload(doc_payload)
	doc_payload["doctype"] = "Payment Entry"
	doc = frappe.get_doc(doc_payload)
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()
	result = {
		"name": doc.name,
		"docstatus": int(doc.docstatus or 0),
		"payment_type": doc.get("payment_type"),
		"party_type": doc.get("party_type"),
		"party": doc.get("party"),
		"paid_amount": _coerce_float(doc.get("paid_amount"), 0.0),
		"received_amount": _coerce_float(doc.get("received_amount"), 0.0),
		"unallocated_amount": _coerce_float(doc.get("unallocated_amount"), 0.0),
		"posting_date": str(doc.get("posting_date")) if doc.get("posting_date") else None,
		"modified": str(doc.get("modified")) if doc.get("modified") else None,
	}

	return ok(result)
