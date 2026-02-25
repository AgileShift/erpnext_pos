"""Endpoints de clientes: listado resumido, cartera y upsert atómico."""

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
from .settings import enforce_doctype_permission


_OUTSTANDING_STATUSES = (
	"Unpaid",
	"Overdue",
	"Partly Paid",
	"Overdue and Discounted",
	"Unpaid and Discounted",
	"Partly Paid and Discounted",
)


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
	territory = str(value_from_aliases(body, "territory", default="") or "").strip()
	route = str(value_from_aliases(body, "route", default="") or "").strip()
	profile_name = str(
		value_from_aliases(body, "pos_profile", "posProfile", "profile_name", "profileName", default="") or ""
	).strip() or None
	company_name = str(value_from_aliases(body, "company", "company_name", "companyName", default="") or "").strip() or None
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
					"disabled": 1 if to_bool(row.get("disabled"), default=False) else 0,
					"credit_limits": credit_limits,
					"primary_address": row.get("primary_address"),
					"email_id": row.get("email_id"),
					"image": row.get("image"),
					"outstanding": outstanding,
					"total_outstanding": outstanding,
					"currentBalance": outstanding,
					"pending_invoices_count": pending_count,
					"pendingInvoices": pending_count,
					"totalPendingAmount": outstanding,
					"pendingInvoicesCount": pending_count,
					"available_credit": available_credit,
					"availableCredit": available_credit,
				}
			)
	return ok(data)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def outstanding(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)
	customer = str(value_from_aliases(body, "customer", default="") or "").strip()
	pos_profile = str(value_from_aliases(body, "pos_profile", "posProfile", default="") or "").strip()
	company_name = str(value_from_aliases(body, "company", "company_name", "companyName", default="") or "").strip() or None
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
			"totalOutstanding": total,
			"outstanding": total,
			"currentBalance": total,
			"pendingInvoicesCount": len(filtered_invoices),
			"pending_invoices_count": len(filtered_invoices),
			"totalPendingAmount": total,
			"pendingInvoices": filtered_invoices,
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
		"customer_name": value_from_aliases(body, "customerName", "customer_name"),
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
	route = value_from_aliases(body, "route")
	if route and "route" in customer_fields:
		values["route"] = route

	return {
		key: value
		for key, value in values.items()
		if value is not None and (not isinstance(value, str) or bool(value.strip()))
	}


def _find_existing_customer(body: dict[str, Any], customer_name: str, mobile_no: str | None) -> str | None:
	for key in ("name", "customer", "customer_id", "customerId"):
		name = str(value_from_aliases(body, key, default="") or "").strip()
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
			company = str(value_from_aliases(raw_row, "company", default=fallback_company) or "").strip()
			credit_limit_value = value_from_aliases(raw_row, "credit_limit", "creditLimit")
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
					if to_bool(
						value_from_aliases(raw_row, "bypass_credit_limit_check", "bypassCreditLimitCheck"),
						default=False,
					)
					else 0,
				},
			)
			rows_added += 1

	if rows_added == 0:
		scalar_credit = value_from_aliases(body, "creditLimit", "credit_limit")
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

	address_line1 = value_from_aliases(address, "line1", "address_line1", default=value_from_aliases(body, "address_line1"))
	address_line2 = value_from_aliases(address, "line2", "address_line2", default=value_from_aliases(body, "address_line2"))
	address_city = value_from_aliases(address, "city", default=value_from_aliases(body, "city"))
	address_state = value_from_aliases(address, "state", default=value_from_aliases(body, "state"))
	address_country = value_from_aliases(address, "country", default=value_from_aliases(body, "country"))
	address_title = value_from_aliases(
		address,
		"address_title",
		"addressTitle",
		default=customer_doc.get("customer_name") or customer_doc.name,
	) or (customer_doc.get("customer_name") or customer_doc.name)
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
	if not has_address_payload:
		return None

	existing_name = _find_linked_parent("Address", customer_doc.name)
	if existing_name and frappe.db.exists("Address", existing_name):
		address_doc = frappe.get_doc("Address", existing_name)
		enforce_doctype_permission("Address", "write", doc=address_doc)
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
	if "primary_address" in customer_fields:
		customer_doc.set("primary_address", address_doc.name)
		customer_doc.save(ignore_permissions=True)
	return address_doc.name


def _upsert_customer_contact(customer_doc, body: dict[str, Any]) -> str | None:
	contact = body.get("contact")
	if not isinstance(contact, dict):
		contact = {}

	contact_email = value_from_aliases(contact, "email", "email_id", default=value_from_aliases(body, "email", "email_id"))
	contact_mobile = value_from_aliases(
		contact, "mobile", "mobile_no", default=value_from_aliases(body, "mobileNo", "mobile_no")
	)
	contact_phone = value_from_aliases(contact, "phone", default=value_from_aliases(body, "phone"))
	if not any((contact_email, contact_mobile, contact_phone)):
		return None

	existing_name = _find_linked_parent("Contact", customer_doc.name)
	if existing_name and frappe.db.exists("Contact", existing_name):
		contact_doc = frappe.get_doc("Contact", existing_name)
		enforce_doctype_permission("Contact", "write", doc=contact_doc)
		for fieldname, value in (
			("first_name", value_from_aliases(contact, "first_name", "firstName", default=customer_doc.get("customer_name"))),
			("email_id", contact_email),
			("mobile_no", contact_mobile),
			("phone", contact_phone),
		):
			if value is None:
				continue
			contact_doc.set(fieldname, value)
		contact_doc.save(ignore_permissions=True)
		return contact_doc.name

	enforce_doctype_permission("Contact", "create")
	contact_doc = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": value_from_aliases(contact, "first_name", "firstName", default=customer_doc.get("customer_name"))
			or customer_doc.get("customer_name")
			or customer_doc.name,
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
def upsert_atomic(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
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
	customer_mobile = str(value_from_aliases(body, "mobileNo", "mobile_no", "phone", default="") or "").strip() or None
	existing_customer_name = _find_existing_customer(body, customer_name, customer_mobile)
	customer_fields = _customer_fieldnames()
	values = _normalize_customer_values(body, customer_fields)

	is_create = not bool(existing_customer_name)
	if is_create:
		enforce_doctype_permission("Customer", "create")
		if not customer_name:
			frappe.throw("customer_name is required")
		customer_doc = frappe.get_doc({"doctype": "Customer"})
	else:
		customer_doc = frappe.get_doc("Customer", existing_customer_name)
		enforce_doctype_permission("Customer", "write", doc=customer_doc)

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
	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		response,
		reference_doctype="Customer",
		reference_name=customer_doc.name,
	)
	return ok(response, request_id=request_id)
