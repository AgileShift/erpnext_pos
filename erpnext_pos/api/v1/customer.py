from __future__ import annotations

from typing import Any

import frappe

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


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def list_with_summary(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	territory = str(value_from_aliases(body, "territory", default="") or "").strip()
	route = str(value_from_aliases(body, "route", default="") or "").strip()
	customer_fields = set(frappe.get_all("DocField", filters={"parent": "Customer"}, pluck="fieldname", page_length=0))
	filters: dict[str, Any] = {"disabled": 0}
	if route and "route" in customer_fields:
		filters["route"] = route
	elif territory:
		filters["territory"] = territory

	selected_fields = ["name"]
	for fieldname in (
		"customer_name",
		"route",
		"territory",
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

	data = []
	for row in customers:
		data.append(
			{
				"name": row.get("name"),
				"customer_name": row.get("customer_name"),
				"route": row.get("route"),
				"territory": row.get("territory"),
				"mobile_no": row.get("mobile_no"),
				"customer_type": row.get("customer_type") or "Individual",
				"disabled": 1 if to_bool(row.get("disabled"), default=False) else 0,
				"credit_limits": credits_by_customer.get(row.get("name"), []),
				"primary_address": row.get("primary_address"),
				"email_id": row.get("email_id"),
				"image": row.get("image"),
			}
		)
	return ok(data)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def outstanding(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	customer = str(value_from_aliases(body, "customer", default="") or "").strip()
	pos_profile = str(value_from_aliases(body, "pos_profile", "posProfile", default="") or "").strip()
	if not customer:
		frappe.throw("customer is required")
	if not pos_profile:
		frappe.throw("pos_profile is required")

	statuses = [
		"Unpaid",
		"Overdue",
		"Partly Paid",
		"Overdue and Discounted",
		"Unpaid and Discounted",
		"Partly Paid and Discounted",
	]
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "pos_profile": pos_profile, "status": ["in", statuses]},
		fields=["name", "posting_date", "due_date", "grand_total", "outstanding_amount", "status", "paid_amount"],
		order_by="posting_date desc",
		page_length=0,
	)
	total = 0.0
	for row in invoices:
		total += float(row.get("outstanding_amount") or (row.get("grand_total") or 0) - (row.get("paid_amount") or 0))
	return ok({"totalOutstanding": total, "pendingInvoices": invoices})


@frappe.whitelist(methods=["POST"])
@standard_api_response
def upsert_atomic(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
	enforce_api_access()
	enforce_doctype_permission("Customer", "create")
	body = parse_payload(payload)
	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "customer.upsert_atomic"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	customer_name = str(value_from_aliases(body, "customerName", "customer_name", default="") or "").strip()
	if not customer_name:
		frappe.throw("customerName is required")

	customer_fieldnames = set(frappe.get_all("DocField", filters={"parent": "Customer"}, pluck="fieldname", page_length=0))
	customer_doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": customer_name,
			"customer_type": value_from_aliases(body, "customerType", "customer_type", default="Individual"),
			"customer_group": value_from_aliases(body, "customerGroup", "customer_group"),
			"territory": value_from_aliases(body, "territory"),
			"default_currency": value_from_aliases(body, "defaultCurrency", "default_currency"),
			"default_price_list": value_from_aliases(body, "defaultPriceList", "default_price_list"),
			"mobile_no": value_from_aliases(body, "mobileNo", "mobile_no", "phone"),
			"email_id": value_from_aliases(body, "email", "email_id"),
			"tax_id": value_from_aliases(body, "taxId", "tax_id"),
			"tax_category": value_from_aliases(body, "taxCategory", "tax_category"),
			"is_internal_customer": 1
			if to_bool(value_from_aliases(body, "isInternalCustomer", "is_internal_customer"), default=False)
			else 0,
			"represents_company": value_from_aliases(body, "representsCompany", "represents_company"),
			"payment_terms": value_from_aliases(body, "paymentTerms", "payment_terms"),
			"customer_details": value_from_aliases(body, "notes", "customer_details"),
		}
	)
	route = value_from_aliases(body, "route")
	if route and "route" in customer_fieldnames:
		customer_doc.set("route", route)
	customer_doc.insert(ignore_permissions=True)

	fallback_company = value_from_aliases(body, "representsCompany", "represents_company") or frappe.defaults.get_user_default(
		"Company"
	)
	credit_rows_added = 0
	raw_credit_limits = body.get("credit_limits") or body.get("creditLimits")
	if isinstance(raw_credit_limits, list):
		for raw_row in raw_credit_limits:
			if not isinstance(raw_row, dict):
				continue
			company = value_from_aliases(raw_row, "company", default=fallback_company)
			credit_limit_value = value_from_aliases(raw_row, "credit_limit", "creditLimit")
			if not company or credit_limit_value is None:
				continue
			try:
				credit_limit_float = float(credit_limit_value)
			except Exception:
				continue
			customer_doc.append(
				"credit_limits",
				{
					"company": company,
					"credit_limit": credit_limit_float,
					"bypass_credit_limit_check": 1
					if to_bool(
						value_from_aliases(raw_row, "bypass_credit_limit_check", "bypassCreditLimitCheck"),
						default=False,
					)
					else 0,
				},
			)
			credit_rows_added += 1

	if credit_rows_added == 0:
		credit_limit = value_from_aliases(body, "creditLimit", "credit_limit")
		if credit_limit is not None and fallback_company:
			try:
				credit_limit_float = float(credit_limit)
			except Exception:
				credit_limit_float = None
			if credit_limit_float is not None:
				customer_doc.append(
					"credit_limits",
					{"company": fallback_company, "credit_limit": credit_limit_float},
				)
				credit_rows_added += 1

	if credit_rows_added > 0:
		customer_doc.save(ignore_permissions=True)

	address_name = None
	address = body.get("address")
	if not isinstance(address, dict):
		address = {}
	address_line1 = value_from_aliases(address, "line1", "address_line1", default=value_from_aliases(body, "address_line1"))
	address_line2 = value_from_aliases(address, "line2", "address_line2", default=value_from_aliases(body, "address_line2"))
	address_city = value_from_aliases(address, "city", default=value_from_aliases(body, "city"))
	address_state = value_from_aliases(address, "state", default=value_from_aliases(body, "state"))
	address_country = value_from_aliases(address, "country", default=value_from_aliases(body, "country"))
	address_title = value_from_aliases(address, "address_title", "addressTitle", default=customer_name) or customer_name
	address_type = value_from_aliases(address, "address_type", "addressType", default="Billing") or "Billing"
	address_email = value_from_aliases(
		address, "email", "email_id", default=value_from_aliases(body, "email", "email_id")
	)
	address_phone = value_from_aliases(
		address,
		"phone",
		default=value_from_aliases(body, "mobileNo", "mobile_no", "phone"),
	)
	has_address_payload = any((address_line1, address_line2, address_city, address_state, address_country))
	if has_address_payload:
		if not address_line1 or not address_city:
			frappe.throw("address.address_line1 and address.city are required when address is provided")
		enforce_doctype_permission("Address", "create")
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
		address_name = address_doc.name

	contact_name = None
	contact = body.get("contact")
	if not isinstance(contact, dict):
		contact = {}
	contact_email = value_from_aliases(contact, "email", "email_id", default=value_from_aliases(body, "email", "email_id"))
	contact_mobile = value_from_aliases(
		contact, "mobile", "mobile_no", default=value_from_aliases(body, "mobileNo", "mobile_no")
	)
	contact_phone = value_from_aliases(contact, "phone", default=value_from_aliases(body, "phone"))
	if any((contact_email, contact_mobile, contact_phone)):
		enforce_doctype_permission("Contact", "create")
		contact_doc = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": value_from_aliases(contact, "first_name", "firstName", default=customer_name) or customer_name,
				"email_id": contact_email,
				"mobile_no": contact_mobile,
				"phone": contact_phone,
				"links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
			}
		)
		contact_doc.insert(ignore_permissions=True)
		contact_name = contact_doc.name

	response = {
		"name": customer_doc.name,
		"customer": customer_doc.name,
		"customer_name": customer_doc.name,
		"contact_name": contact_name,
		"address_name": address_name,
	}
	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		response,
		reference_doctype="Customer",
		reference_name=customer_doc.name,
	)
	return ok(response, request_id=request_id)
