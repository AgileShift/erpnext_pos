"""Endpoints de clientes: listado resumido, cartera y upsert atómico."""

from typing import Any

import frappe

from .common import (
	ok,
	parse_payload,
	standard_api_response,
)


_OUTSTANDING_STATUSES = (
	"Unpaid",
	"Overdue",
	"Partly Paid",
	"Overdue and Discounted",
	"Unpaid and Discounted",
	"Partly Paid and Discounted",
)
def _as_bool(value: Any, default: bool = False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	if isinstance(value, str):
		normalized = value.strip().lower()
		if normalized in {"1", "true", "yes", "y", "on"}:
			return True
		if normalized in {"0", "false", "no", "n", "off", ""}:
			return False
	return default


def _invoice_matches_profile(row: dict[str, Any], profile_name: str | None) -> bool:
	if not profile_name:
		return True
	row_profile = str(row.get("pos_profile") or "").strip()
	# Se incluyen facturas sin pos_profile para cubrir documentos creados desde Desk.
	return (not row_profile) or row_profile == profile_name


def _get_profile_company(profile_name: str | None) -> str | None:
	profile = str(profile_name or "").strip()
	if not profile or not frappe.db.exists("POS Profile", profile):
		return None
	return str(frappe.db.get_value("POS Profile", profile, "company") or "").strip() or None


def _resolve_credit_limit(credit_limits: list[dict[str, Any]], company_name: str | None) -> float | None:
	if not credit_limits:
		return None
	if company_name:
		for row in credit_limits:
			company = str(row.get("company") or "").strip()
			if company and company == company_name:
				try:
					return float(row.get("credit_limit"))
				except Exception:
					return None
	for row in credit_limits:
		try:
			return float(row.get("credit_limit"))
		except Exception:
			continue
	return None


def _get_customer_outstanding_summary(
	customer_names: list[str],
	*,
	profile_name: str | None,
	company_name: str | None,
) -> dict[str, dict[str, float | int]]:
	if not customer_names:
		return {}

	filters: dict[str, Any] = {
		"customer": ["in", customer_names],
		"status": ["in", list(_OUTSTANDING_STATUSES)],
	}
	if company_name:
		filters["company"] = company_name

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=["name", "customer", "company", "pos_profile", "grand_total", "paid_amount", "outstanding_amount"],
		page_length=0,
	)
	summary: dict[str, dict[str, float | int]] = {}
	for row in rows:
		if not _invoice_matches_profile(row, profile_name):
			continue
		customer = str(row.get("customer") or "").strip()
		if not customer:
			continue
		try:
			outstanding = float(
				row.get("outstanding_amount")
				or (row.get("grand_total") or 0) - (row.get("paid_amount") or 0)
			)
		except Exception:
			outstanding = 0.0
		if outstanding <= 0:
			continue
		bucket = summary.setdefault(customer, {"outstanding": 0.0, "pending_invoices_count": 0})
		bucket["outstanding"] = float(bucket["outstanding"]) + outstanding
		bucket["pending_invoices_count"] = int(bucket["pending_invoices_count"]) + 1

	return summary


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def list_with_summary(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)
	territory = str(body.get("territory") or "").strip()
	route = str(body.get("route") or "").strip()
	profile_name = str(body.get("pos_profile") or body.get("profile_name") or "").strip() or None
	company_name = str(body.get("company") or body.get("company_name") or "").strip() or None
	if not company_name:
		company_name = _get_profile_company(profile_name)
	customer_fields = set(frappe.get_all("DocField", filters={"parent": "Customer"}, pluck="fieldname", page_length=0))
	filters: dict[str, Any] = {"disabled": 0}
	if route and "route" in customer_fields:
		filters["route"] = route
	elif territory and "territory" in customer_fields:
		filters["territory"] = territory

	selected_fields = ["name"]
	for fieldname in (
		"customer_name",
		"route",
		"territory",
		"customer_group",
		"default_currency",
		"default_price_list",
		"mobile_no",
		"primary_address",
		"email_id",
		"image",
		"customer_type",
		"disabled",
	):
		if fieldname in customer_fields:
			selected_fields.append(fieldname)

	customers = frappe.get_all(
		"Customer",
		filters=filters,
		fields=selected_fields,
		order_by="customer_name asc",
		page_length=0,
	)

	customer_names = [row.get("name") for row in customers if row.get("name")]
	credit_rows = []
	if customer_names:
		credit_rows = frappe.get_all(
			"Customer Credit Limit",
			filters={"parent": ["in", customer_names]},
			fields=["parent", "company", "credit_limit", "bypass_credit_limit_check"],
			page_length=0,
		)
	credits_by_customer: dict[str, list[dict[str, Any]]] = {}
	for row in credit_rows:
		parent = row.get("parent")
		if not parent:
			continue
		credits_by_customer.setdefault(parent, []).append(
			{
				"company": row.get("company"),
				"credit_limit": row.get("credit_limit"),
				"bypass_credit_limit_check": row.get("bypass_credit_limit_check"),
			}
		)

	outstanding_by_customer = _get_customer_outstanding_summary(
		customer_names=customer_names,
		profile_name=profile_name,
		company_name=company_name,
	)

	data = []
	for row in customers:
		customer_name = row.get("name")
		credit_limits = credits_by_customer.get(customer_name, [])
		summary = outstanding_by_customer.get(customer_name, {"outstanding": 0.0, "pending_invoices_count": 0})
		outstanding = float(summary.get("outstanding") or 0.0)
		pending_count = int(summary.get("pending_invoices_count") or 0)
		credit_limit = _resolve_credit_limit(credit_limits, company_name)
		available_credit = (credit_limit - outstanding) if credit_limit is not None else None
		data.append(
				{
					"name": customer_name,
					"customer_name": row.get("customer_name"),
					"route": row.get("route"),
					"territory": row.get("territory"),
					"customer_group": row.get("customer_group"),
					"default_currency": row.get("default_currency"),
					"default_price_list": row.get("default_price_list"),
					"mobile_no": row.get("mobile_no"),
					"customer_type": row.get("customer_type") or "Individual",
					"disabled": 1 if _as_bool(row.get("disabled"), default=False) else 0,
					"credit_limits": credit_limits,
					"primary_address": row.get("primary_address"),
					"email_id": row.get("email_id"),
					"image": row.get("image"),
					"outstanding": outstanding,
					"total_outstanding": outstanding,
					"pending_invoices_count": pending_count,
					"available_credit": available_credit,
				}
			)
	return ok(data)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def outstanding(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)
	customer = str(body.get("customer") or "").strip()
	pos_profile = str(body.get("pos_profile") or "").strip()
	company_name = str(body.get("company") or body.get("company_name") or "").strip() or None
	if not customer:
		frappe.throw("customer is required")
	if not pos_profile:
		frappe.throw("pos_profile is required")
	if not company_name:
		company_name = _get_profile_company(pos_profile)

	filters: dict[str, Any] = {"customer": customer, "status": ["in", list(_OUTSTANDING_STATUSES)]}
	if company_name:
		filters["company"] = company_name

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"due_date",
			"grand_total",
			"outstanding_amount",
			"status",
			"paid_amount",
			"pos_profile",
			"company",
			"currency",
			"customer",
			"customer_name",
		],
		order_by="posting_date desc",
		page_length=0,
	)
	filtered_invoices: list[dict[str, Any]] = []
	total = 0.0
	for row in invoices:
		if not _invoice_matches_profile(row, pos_profile):
			continue
		outstanding_amount = float(
			row.get("outstanding_amount") or (row.get("grand_total") or 0) - (row.get("paid_amount") or 0)
		)
		if outstanding_amount <= 0:
			continue
		row["outstanding_amount"] = outstanding_amount
		filtered_invoices.append(row)
		total += outstanding_amount
	return ok(
		{
			"outstanding": total,
			"pending_invoices_count": len(filtered_invoices),
			"pending_invoices": filtered_invoices,
		}
	)


def _customer_fieldnames() -> set[str]:
	return set(frappe.get_all("DocField", filters={"parent": "Customer"}, pluck="fieldname", page_length=0))


def _coerce_float(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except Exception:
		return default


def _normalize_customer_values(body: dict[str, Any], customer_fields: set[str]) -> dict[str, Any]:
	values = {
		"customer_name": body.get("customer_name"),
		"customer_type": body.get("customer_type") or "Individual",
		"customer_group": body.get("customer_group"),
		"territory": body.get("territory"),
		"default_currency": body.get("default_currency"),
		"default_price_list": body.get("default_price_list"),
		"mobile_no": body.get("mobile_no") or body.get("phone"),
		"email_id": body.get("email") or body.get("email_id"),
		"tax_id": body.get("tax_id"),
		"tax_category": body.get("tax_category"),
		"is_internal_customer": 1
		if _as_bool(body.get("is_internal_customer"), default=False)
		else 0,
		"represents_company": body.get("represents_company"),
		"payment_terms": body.get("payment_terms"),
		"customer_details": body.get("notes") or body.get("customer_details"),
	}
	route = body.get("route")
	if route and "route" in customer_fields:
		values["route"] = route

	return {
		key: value
		for key, value in values.items()
		if value is not None and (not isinstance(value, str) or bool(value.strip()))
	}


def _find_existing_customer(body: dict[str, Any], customer_name: str, mobile_no: str | None) -> str | None:
	for key in ("name", "customer", "customer_id"):
		name = str(body.get(key) or "").strip()
		if name and frappe.db.exists("Customer", name):
			return name

	if customer_name and mobile_no:
		match = frappe.get_all(
			"Customer",
			filters={"customer_name": customer_name, "mobile_no": mobile_no},
			pluck="name",
			limit_page_length=1,
		)
		if match:
			return match[0]

	if customer_name:
		match = frappe.get_all(
			"Customer",
			filters={"customer_name": customer_name},
			pluck="name",
			limit_page_length=1,
		)
		if match:
			return match[0]

	return None


def _replace_credit_limits(customer_doc, body: dict[str, Any], *, fallback_company: str | None) -> None:
	has_credit_payload = any(key in body for key in ("credit_limits", "creditLimits", "creditLimit", "credit_limit"))
	if not has_credit_payload:
		return

	customer_doc.set("credit_limits", [])
	raw_credit_limits = body.get("credit_limits") or body.get("creditLimits")
	rows_added = 0
	if isinstance(raw_credit_limits, list):
		for raw_row in raw_credit_limits:
			if not isinstance(raw_row, dict):
				continue
			company = str(raw_row.get("company") or fallback_company or "").strip()
			credit_limit_value = raw_row.get("credit_limit")
			if not company or credit_limit_value is None:
				continue

			credit_limit = _coerce_float(credit_limit_value, -1.0)
			if credit_limit < 0:
				continue

			customer_doc.append(
				"credit_limits",
				{
					"company": company,
					"credit_limit": credit_limit,
					"bypass_credit_limit_check": 1
					if _as_bool(raw_row.get("bypass_credit_limit_check"), default=False)
					else 0,
				},
			)
			rows_added += 1

	if rows_added == 0:
		scalar_credit = body.get("credit_limit")
		if scalar_credit is not None and fallback_company:
			credit_limit = _coerce_float(scalar_credit, -1.0)
			if credit_limit >= 0:
				customer_doc.append(
					"credit_limits",
					{"company": fallback_company, "credit_limit": credit_limit},
				)

	customer_doc.save(ignore_permissions=True)


def _find_linked_parent(parenttype: str, customer_name: str) -> str | None:
	if not frappe.db.exists("DocType", "Dynamic Link"):
		return None
	return frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer_name, "parenttype": parenttype},
		"parent",
	)


def _upsert_customer_address(customer_doc, body: dict[str, Any], customer_fields: set[str]) -> str | None:
	address = body.get("address")
	if not isinstance(address, dict):
		address = {}

	address_line1 = address.get("address_line1") or address.get("line1") or body.get("address_line1")
	address_line2 = address.get("address_line2") or address.get("line2") or body.get("address_line2")
	address_city = address.get("city") or body.get("city")
	address_state = address.get("state") or body.get("state")
	address_country = address.get("country") or body.get("country")
	address_title = address.get("address_title") or customer_doc.get("customer_name") or customer_doc.name
	address_type = address.get("address_type") or "Billing"
	address_email = address.get("email") or address.get("email_id") or body.get("email") or body.get("email_id")
	address_phone = address.get("phone") or body.get("mobile_no") or body.get("phone")
	has_address_payload = any((address_line1, address_line2, address_city, address_state, address_country))
	if not has_address_payload:
		return None

	existing_name = _find_linked_parent("Address", customer_doc.name)
	if existing_name and frappe.db.exists("Address", existing_name):
		address_doc = frappe.get_doc("Address", existing_name)
		for fieldname, value in (
			("address_title", address_title),
			("address_type", address_type),
			("address_line1", address_line1),
			("address_line2", address_line2),
			("city", address_city),
			("state", address_state),
			("country", address_country),
			("email_id", address_email),
			("phone", address_phone),
		):
			if value is None:
				continue
			address_doc.set(fieldname, value)
		address_doc.save(ignore_permissions=True)
		return address_doc.name

	if not address_line1 or not address_city:
		frappe.throw("address.address_line1 and address.city are required when creating address")

	address_doc = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": address_title,
			"address_type": address_type,
			"address_line1": address_line1,
			"address_line2": address_line2,
			"city": address_city,
			"state": address_state,
			"country": address_country,
			"email_id": address_email,
			"phone": address_phone,
			"links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
		}
	)
	address_doc.insert(ignore_permissions=True)
	if "primary_address" in customer_fields:
		customer_doc.set("primary_address", address_doc.name)
		customer_doc.save(ignore_permissions=True)
	return address_doc.name


def _upsert_customer_contact(customer_doc, body: dict[str, Any]) -> str | None:
	contact = body.get("contact")
	if not isinstance(contact, dict):
		contact = {}

	contact_email = contact.get("email") or contact.get("email_id") or body.get("email") or body.get("email_id")
	contact_mobile = contact.get("mobile_no") or contact.get("mobile") or body.get("mobile_no")
	contact_phone = contact.get("phone") or body.get("phone")
	if not any((contact_email, contact_mobile, contact_phone)):
		return None

	existing_name = _find_linked_parent("Contact", customer_doc.name)
	if existing_name and frappe.db.exists("Contact", existing_name):
		contact_doc = frappe.get_doc("Contact", existing_name)
		for fieldname, value in (
			("first_name", contact.get("first_name") or customer_doc.get("customer_name")),
			("email_id", contact_email),
			("mobile_no", contact_mobile),
			("phone", contact_phone),
		):
			if value is None:
				continue
			contact_doc.set(fieldname, value)
		contact_doc.save(ignore_permissions=True)
		return contact_doc.name

	contact_doc = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": contact.get("first_name") or customer_doc.get("customer_name") or customer_doc.name,
			"email_id": contact_email,
			"mobile_no": contact_mobile,
			"phone": contact_phone,
			"links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
		}
	)
	contact_doc.insert(ignore_permissions=True)
	return contact_doc.name


@frappe.whitelist(methods=["POST"])
@standard_api_response
def upsert_atomic(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)

	customer_name = str(body.get("customer_name") or "").strip()
	customer_mobile = str(body.get("mobile_no") or body.get("phone") or "").strip() or None
	existing_customer_name = _find_existing_customer(body, customer_name, customer_mobile)
	customer_fields = _customer_fieldnames()
	values = _normalize_customer_values(body, customer_fields)

	is_create = not bool(existing_customer_name)
	if is_create:
		if not customer_name:
			frappe.throw("customer_name is required")
		customer_doc = frappe.get_doc({"doctype": "Customer"})
	else:
		customer_doc = frappe.get_doc("Customer", existing_customer_name)

	for key, value in values.items():
		customer_doc.set(key, value)

	if is_create:
		customer_doc.insert(ignore_permissions=True)
	else:
		customer_doc.save(ignore_permissions=True)

	fallback_company = values.get("represents_company") or frappe.defaults.get_user_default("Company")
	_replace_credit_limits(customer_doc, body, fallback_company=fallback_company)

	address_name = _upsert_customer_address(customer_doc, body, customer_fields)
	contact_name = _upsert_customer_contact(customer_doc, body)
	customer_doc.reload()

	response = {
		"name": customer_doc.name,
		"customer": customer_doc.name,
		"customer_name": customer_doc.get("customer_name"),
		"contact_name": contact_name,
		"address_name": address_name,
		"created": 1 if is_create else 0,
		"modified": str(customer_doc.get("modified")) if customer_doc.get("modified") else None,
	}
	return ok(response)
