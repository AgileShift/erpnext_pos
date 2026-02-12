from __future__ import annotations

from typing import Any
from urllib.parse import quote

import frappe
from frappe.utils.data import add_days, get_url, nowdate

from .common import ok, parse_payload, standard_api_response, to_bool, value_from_aliases
from .inventory import _apply_inventory_visibility_rules, _build_inventory_alerts, _build_inventory_items
from .settings import enforce_api_access


def _get_doctype_fieldnames(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	return set(frappe.get_all("DocField", filters={"parent": doctype}, pluck="fieldname", page_length=0))


def _get_queryable_fields(doctype: str, fields: list[str]) -> list[str]:
	meta_columns = {
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"parent",
		"parentfield",
		"parenttype",
	}
	allowed = _get_doctype_fieldnames(doctype) | meta_columns
	result: list[str] = []
	for fieldname in fields:
		raw = (fieldname or "").strip()
		if not raw:
			continue
		base = raw
		lower_raw = raw.lower()
		if " as " in lower_raw:
			base = raw[: lower_raw.index(" as ")].strip()
		base = base.replace("`", "").strip()
		if base in allowed:
			result.append(raw)
	return result or ["name"]


def _safe_get_all(
	doctype: str,
	*,
	fields: list[str],
	filters: dict[str, Any] | None = None,
	order_by: str | None = None,
	page_length: int = 0,
	limit_page_length: int | None = None,
) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", doctype):
		return []
	query_fields = _get_queryable_fields(doctype, fields)
	return frappe.get_all(
		doctype,
		filters=filters,
		fields=query_fields,
		order_by=order_by,
		page_length=page_length,
		limit_page_length=limit_page_length,
	)


def _get_single_row(doctype: str, fields: list[str], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
	if not frappe.db.exists("DocType", doctype):
		return defaults or {}
	fieldnames = _get_doctype_fieldnames(doctype)
	row = dict(defaults or {})
	for fieldname in fields:
		if fieldname not in fieldnames:
			row.setdefault(fieldname, defaults.get(fieldname) if defaults else None)
			continue
		row[fieldname] = frappe.db.get_single_value(doctype, fieldname)
	return row


def _require_open_shift(profile_name: str | None, opening_name: str | None) -> dict[str, Any]:
	"""Ensure the API user has an active POS Opening Entry before bootstrap."""
	if not frappe.db.exists("DocType", "POS Opening Entry"):
		frappe.throw("POS Opening Entry is not available in this site")

	fieldnames = _get_doctype_fieldnames("POS Opening Entry")
	base_filters: dict[str, Any] = {"docstatus": 1}
	if "user" in fieldnames:
		base_filters["user"] = frappe.session.user
	if profile_name and "pos_profile" in fieldnames:
		base_filters["pos_profile"] = profile_name

	query_fields = [
		"name",
		"status",
		"pos_profile",
		"company",
		"user",
		"period_start_date",
		"period_end_date",
		"posting_date",
		"pos_closing_entry",
		"modified",
	]

	filters = dict(base_filters)
	if opening_name:
		filters["name"] = opening_name
	rows = _safe_get_all(
		"POS Opening Entry",
		filters=filters,
		fields=query_fields,
		order_by="modified desc",
		page_length=20,
	)

	open_entry = None
	for row in rows:
		status = str(row.get("status") or "").strip().lower()
		if "status" in fieldnames:
			if status == "open":
				open_entry = row
				break
			continue
		# Compatibility fallback: if status does not exist, treat rows without closing link as open.
		if not row.get("pos_closing_entry"):
			open_entry = row
			break

	if open_entry:
		return open_entry

	if opening_name:
		any_filters = dict(base_filters)
		any_filters["name"] = opening_name
		existing = _safe_get_all(
			"POS Opening Entry",
			filters=any_filters,
			fields=query_fields,
			order_by="modified desc",
			page_length=1,
		)
		if existing:
			status = existing[0].get("status") or "Unknown"
			frappe.throw(
				f"POS Opening Entry {opening_name} is not open (status: {status}). Open a new shift first."
			)

	frappe.throw("Open shift required. Call pos_session.opening_create_submit before sync.bootstrap.")


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def my_pos_profiles(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	parse_payload(payload)
	user = frappe.session.user
	profiles = _get_accessible_pos_profiles(user)
	default_profile = None

	if frappe.db.exists("DocType", "POS Profile User"):
		pfu_fields = _get_doctype_fieldnames("POS Profile User")
		if "user" in pfu_fields and "parent" in pfu_fields:
			fields = ["parent"]
			if "default" in pfu_fields:
				fields.append("`default`")
			filters: dict[str, Any] = {"user": user}
			if "parenttype" in pfu_fields:
				filters["parenttype"] = "POS Profile"
			assignments = frappe.get_all("POS Profile User", filters=filters, fields=fields, page_length=0)
			default_map = {row.get("parent"): int(row.get("default") or 0) for row in assignments if row.get("parent")}
			for profile in profiles:
				name = profile.get("name")
				profile["is_default"] = bool(default_map.get(name, 0))
				if profile["is_default"] and not default_profile:
					default_profile = name
		else:
			for profile in profiles:
				profile["is_default"] = False
	else:
		for profile in profiles:
			profile["is_default"] = False

	return ok(
		{
			"user": user,
			"default_profile": default_profile,
			"profiles": profiles,
			"count": len(profiles),
		}
	)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def bootstrap(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)
	settings = enforce_api_access()

	include_inventory = to_bool(value_from_aliases(body, "include_inventory", "includeInventory"), default=True)
	include_customers = to_bool(value_from_aliases(body, "include_customers", "includeCustomers"), default=True)
	include_invoices = to_bool(value_from_aliases(body, "include_invoices", "includeInvoices"), default=True)
	include_alerts = to_bool(value_from_aliases(body, "include_alerts", "includeAlerts"), default=True)
	recent_paid_only = to_bool(value_from_aliases(body, "recent_paid_only", "recentPaidOnly"), default=True)
	requested_profile_name = str(
		value_from_aliases(body, "profile_name", "profileName", "pos_profile", "posProfile", default="") or ""
	).strip()
	profile_name = requested_profile_name
	pos_opening_entry_name = str(
		value_from_aliases(
			body,
			"pos_opening_entry",
			"pos_opening_name",
			"posOpeningEntry",
			"posOpeningName",
			default="",
		)
		or ""
	).strip()

	profiles = _get_accessible_pos_profiles(frappe.session.user)
	accessible_profile_names = {row.get("name") for row in profiles if row.get("name")}
	if requested_profile_name and requested_profile_name not in accessible_profile_names:
		frappe.throw(f"User {frappe.session.user} does not have access to POS Profile {requested_profile_name}.")
	if not profile_name and profiles:
		profile_name = profiles[0].get("name")

	open_shift = _require_open_shift(requested_profile_name or None, pos_opening_entry_name or None)
	if not requested_profile_name and open_shift.get("pos_profile"):
		profile_name = open_shift.get("pos_profile")
	if profile_name and profile_name not in accessible_profile_names:
		frappe.throw(f"User {frappe.session.user} does not have access to POS Profile {profile_name}.")

	pos_profile_detail = _get_pos_profile_detail(profile_name) if profile_name else None
	company_name = (
		(pos_profile_detail or {}).get("company")
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_value("Company", {}, "name")
	)
	company = {}
	if company_name:
		company = (
			_safe_get_all(
				"Company",
				filters={"name": company_name},
				fields=["name as company", "default_currency", "country", "tax_id"],
				limit_page_length=1,
			)[0]
			if frappe.db.exists("Company", company_name)
			else {}
		)

	warehouse = str(
		value_from_aliases(body, "warehouse", "warehouse_id", "warehouseId", default=(pos_profile_detail or {}).get("warehouse"))
		or ""
	).strip()
	price_list = str(
		value_from_aliases(
			body,
			"price_list",
			"priceList",
			default=(pos_profile_detail or {}).get("selling_price_list"),
		)
		or ""
	).strip()
	route = (
		(str(value_from_aliases(body, "route", default="") or ""))
		or ((pos_profile_detail or {}).get("route") or "")
		or ((pos_profile_detail or {}).get("territory") or "")
	).strip()
	territory = (
		(str(value_from_aliases(body, "territory", default="") or ""))
		or route
	).strip()

	inventory_items: list[dict[str, Any]] = []
	inventory_alerts: list[dict[str, Any]] = []
	computed_inventory_alerts: list[dict[str, Any]] = []
	if include_inventory and warehouse:
		inventory_items = _build_inventory_items(
			warehouse=warehouse,
			price_list=price_list,
			offset=int(value_from_aliases(body, "offset", default=0) or 0),
			limit=int(value_from_aliases(body, "limit", "page_size", "pageSize", default=(settings.default_sync_page_size or 50)) or 0),
		)
	if warehouse and inventory_items and (include_alerts or _has_negative_inventory_rows(inventory_items)):
		computed_inventory_alerts = _build_inventory_alerts(warehouse=warehouse, items=inventory_items)
		inventory_items = _apply_inventory_visibility_rules(items=inventory_items, alerts=computed_inventory_alerts)
	if include_alerts:
		inventory_alerts = computed_inventory_alerts

	customers = _get_customers(route=route or None, territory=territory or None) if include_customers else []
	invoices = _get_invoices(profile_name, settings, recent_paid_only=recent_paid_only) if include_invoices else []
	payment_entries = _get_payment_entries(
		from_date=str(value_from_aliases(body, "from_date", "fromDate", default=add_days(nowdate(), -30)))
	)
	currency_base = ((company or {}).get("default_currency") or (pos_profile_detail or {}).get("currency") or "").strip()
	exchange_rate_date = str(open_shift.get("posting_date") or nowdate())
	currencies, exchange_rates = _get_active_currencies(
		base_currency=currency_base or None,
		rate_date=exchange_rate_date,
	)
	stock_settings = _get_single_row(
		"Stock Settings",
		fields=["allow_negative_stock"],
		defaults={"allow_negative_stock": 0},
	)
	payment_terms = _safe_get_all(
		"Payment Term",
		fields=[
			"payment_term_name",
			"invoice_portion",
			"mode_of_payment",
			"due_date_based_on",
			"credit_days",
			"credit_months",
			"discount_type",
			"discount",
			"description",
			"discount_validity",
			"discount_validity_based_on",
		],
		page_length=0,
	)
	delivery_charges = _safe_get_all("Delivery Charges", fields=["label", "default_rate"], page_length=0)
	customer_groups = _safe_get_all(
		"Customer Group",
		fields=["name", "customer_group_name", "is_group", "parent_customer_group"],
		page_length=0,
	)
	territories = _safe_get_all(
		"Territory",
		fields=["name", "territory_name", "is_group", "parent_territory"],
		page_length=0,
	)

	data = {
		"context": {
			"profileName": profile_name,
			"profile_name": profile_name,
			"company": company_name,
			"companyCurrency": (company or {}).get("default_currency"),
			"company_currency": (company or {}).get("default_currency"),
			"warehouse": warehouse or None,
			"route": route or None,
			"territory": territory or None,
			"priceList": price_list or None,
			"price_list": price_list or None,
			"currency": (pos_profile_detail or {}).get("currency"),
			"partyAccountCurrency": (company or {}).get("default_currency"),
			"party_account_currency": (company or {}).get("default_currency"),
			"posOpeningEntry": open_shift.get("name"),
			"pos_opening_entry": open_shift.get("name"),
		},
		"open_shift": open_shift,
		"pos_profiles": profiles,
		"pos_profile_detail": pos_profile_detail,
		"company": company,
		"stock_settings": stock_settings or {"allow_negative_stock": 0},
		"currencies": currencies,
		"exchange_rates": exchange_rates,
		"payment_terms": payment_terms,
		"delivery_charges": delivery_charges,
		"customer_groups": customer_groups,
		"territories": territories,
		"inventory_items": inventory_items,
		"inventory_alerts": inventory_alerts,
		"customers": customers,
		"invoices": invoices,
		"payment_entries": payment_entries,
	}
	return ok(data)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def pull_delta(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	modified_since = str(value_from_aliases(body, "modified_since", "modifiedSince", default="") or "").strip()
	if not modified_since:
		frappe.throw("modified_since is required")

	raw_doc_types = value_from_aliases(body, "doctypes", "doc_types", "docTypes") or [
		"Customer",
		"Inventory",
		"Sales Invoice",
		"Payment Entry",
	]
	profile_name = str(
		value_from_aliases(body, "profile_name", "profileName", "pos_profile", "posProfile", default="") or ""
	).strip()
	profile_detail = _get_pos_profile_detail(profile_name) if profile_name else {}
	warehouse = str(
		value_from_aliases(body, "warehouse", "warehouse_id", "warehouseId", default=(profile_detail or {}).get("warehouse"))
		or ""
	).strip()
	price_list = str(
		value_from_aliases(
			body,
			"price_list",
			"priceList",
			default=(profile_detail or {}).get("selling_price_list"),
		)
		or ""
	).strip()
	route = (
		(str(value_from_aliases(body, "route", default="") or ""))
		or ((profile_detail or {}).get("route") or "")
		or ((profile_detail or {}).get("territory") or "")
	).strip()
	territory = ((str(value_from_aliases(body, "territory", default="") or "")) or route).strip()

	inventory_aliases = {
		"inventory",
		"inventory_item",
		"inventory_items",
		"warehouseitem",
		"warehouse_item",
		"warehouse_items",
		"bin",
		"item",
		"item price",
		"item_price",
	}
	customer_aliases = {"customer", "customers"}
	sales_invoice_aliases = {"sales invoice", "sales_invoice", "salesinvoices", "salesinvoicedto"}
	payment_entry_aliases = {"payment entry", "payment_entry", "paymententries", "paymententrydto"}

	doc_types: list[str] = []
	for value in raw_doc_types:
		doctype = str(value or "").strip()
		if not doctype:
			continue
		key = doctype.lower()
		canonical = doctype
		if key in inventory_aliases:
			canonical = "Inventory"
		elif key in customer_aliases:
			canonical = "Customer"
		elif key in sales_invoice_aliases:
			canonical = "Sales Invoice"
		elif key in payment_entry_aliases:
			canonical = "Payment Entry"
		if canonical not in doc_types:
			doc_types.append(canonical)

	result: dict[str, list[dict[str, Any]]] = {}
	for doctype in doc_types:
		if doctype == "Inventory":
			result["Inventory"] = _get_inventory_delta(
				modified_since=modified_since,
				warehouse=warehouse or None,
				price_list=price_list or None,
			)
			continue
		if doctype == "Customer":
			result["Customer"] = _get_customers(
				route=route or None,
				territory=territory or None,
				modified_since=modified_since,
				include_disabled=True,
			)
			continue
		if doctype == "Sales Invoice":
			result["Sales Invoice"] = _get_sales_invoices_delta(
				modified_since=modified_since,
				profile_name=profile_name or None,
			)
			continue
		if doctype == "Payment Entry":
			result["Payment Entry"] = _get_payment_entries_delta(modified_since=modified_since)
			continue

		result[doctype] = _safe_get_all(
			doctype,
			filters={"modified": [">=", modified_since]},
			fields=["name", "modified", "docstatus"],
			order_by="modified asc",
			page_length=0,
		)
	return ok(
		{
			"modified_since": modified_since,
			"changes": result,
			"context": {
				"warehouse": warehouse or None,
				"price_list": price_list or None,
				"route": route or None,
				"territory": territory or None,
				"profile_name": profile_name or None,
			},
		}
	)


def _get_pos_profile_detail(profile_name: str) -> dict[str, Any] | None:
	if not profile_name:
		return None
	if not frappe.db.exists("POS Profile", profile_name):
		return None

	optional_profile_fields = [
		"warehouse",
		"route",
		"territory",
		"country",
		"company",
		"currency",
		"income_account",
		"expense_account",
		"branch",
		"apply_discount_on",
		"cost_center",
		"selling_price_list",
	]
	profile_fieldnames = _get_doctype_fieldnames("POS Profile")
	selected_profile_fields = ["name"] + [
		fieldname for fieldname in optional_profile_fields if fieldname in profile_fieldnames
	]

	row = frappe.get_all(
		"POS Profile",
		filters={"name": profile_name},
		fields=selected_profile_fields,
		limit_page_length=1,
	)
	if not row:
		return None
	profile = row[0]

	for fieldname in optional_profile_fields:
		profile.setdefault(fieldname, "")
	profile["name"] = profile.get("name") or profile_name
	profile["profileName"] = profile.get("name")
	profile["warehouse"] = profile.get("warehouse") or ""
	profile["company"] = profile.get("company") or ""
	profile["currency"] = profile.get("currency") or ""
	profile["apply_discount_on"] = profile.get("apply_discount_on") or "Grand Total"
	profile["selling_price_list"] = profile.get("selling_price_list") or ""

	if not profile.get("country") and profile.get("company"):
		profile["country"] = frappe.db.get_value("Company", profile.get("company"), "country") or ""

	payment_optional_fields = ["default", "mode_of_payment", "allow_in_returns"]
	payment_fieldnames = _get_doctype_fieldnames("POS Payment Method")
	payment_fields = ["name"]
	if "default" in payment_fieldnames:
		payment_fields.append("`default`")
	for fieldname in ("mode_of_payment", "allow_in_returns"):
		if fieldname in payment_fieldnames:
			payment_fields.append(fieldname)

	payments = frappe.get_all(
		"POS Payment Method",
		filters={"parent": profile_name, "parenttype": "POS Profile"},
		fields=payment_fields,
		order_by="idx asc",
		page_length=0,
	)
	for payment in payments:
		for fieldname in payment_optional_fields:
			payment.setdefault(fieldname, 0 if fieldname in {"default", "allow_in_returns"} else "")
		payment["name"] = payment.get("name") or payment.get("mode_of_payment") or ""
		payment["mode_of_payment"] = payment.get("mode_of_payment") or ""
		payment["default"] = 1 if to_bool(payment.get("default"), default=False) else 0
		payment["allow_in_returns"] = 1 if to_bool(payment.get("allow_in_returns"), default=False) else 0
	profile["payments"] = payments
	return profile


def _get_accessible_pos_profiles(user: str) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "POS Profile"):
		return []

	profile_fieldnames = _get_doctype_fieldnames("POS Profile")
	profile_fields = [field for field in ["name", "company", "currency"] if field in (profile_fieldnames | {"name"})]

	# Enforce user mapping from applicable_for_users when child table exists.
	if frappe.db.exists("DocType", "POS Profile User"):
		pfu_fields = _get_doctype_fieldnames("POS Profile User")
		if "user" in pfu_fields and "parent" in pfu_fields:
			pfu_filters: dict[str, Any] = {"user": user}
			if "parenttype" in pfu_fields:
				pfu_filters["parenttype"] = "POS Profile"
			assigned_profiles = frappe.get_all(
				"POS Profile User",
				filters=pfu_filters,
				pluck="parent",
				page_length=0,
			)
			assigned_profiles = sorted({name for name in assigned_profiles if name})
			if not assigned_profiles:
				return []

			rows = _safe_get_all(
				"POS Profile",
				filters={"disabled": 0, "name": ["in", assigned_profiles]},
				fields=profile_fields,
				order_by="name asc",
				page_length=0,
			)
			for row in rows:
				row.setdefault("name", "")
				row["company"] = row.get("company") or ""
				row["currency"] = row.get("currency") or ""
			return rows

	# Compatibility fallback for sites where user mapping is not available.
	rows = _safe_get_all(
		"POS Profile",
		filters={"disabled": 0},
		fields=profile_fields,
		order_by="name asc",
		page_length=0,
	)
	for row in rows:
		row.setdefault("name", "")
		row["company"] = row.get("company") or ""
		row["currency"] = row.get("currency") or ""
	return rows


def _get_active_currencies(
	*,
	base_currency: str | None,
	rate_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
	currencies = _safe_get_all(
		"Currency",
		filters={"enabled": 1},
		fields=["name", "currency_name", "symbol", "number_format"],
		order_by="name asc",
		page_length=0,
	)
	rates: dict[str, float | None] = {}
	for row in currencies:
		currency_name = (row.get("name") or "").strip()
		if not currency_name:
			continue
		exchange_rate = _resolve_exchange_rate(currency_name, base_currency, rate_date)
		row["exchange_rate"] = exchange_rate
		row["exchange_rate_to"] = base_currency
		row["exchange_rate_date"] = rate_date
		rates[currency_name] = exchange_rate
	return currencies, {"base_currency": base_currency, "date": rate_date, "rates": rates}


def _resolve_exchange_rate(from_currency: str, to_currency: str | None, transaction_date: str) -> float | None:
	if not from_currency or not to_currency:
		return None
	if from_currency == to_currency:
		return 1.0

	try:
		from erpnext.setup.utils import get_exchange_rate as erpnext_get_exchange_rate

		rate = erpnext_get_exchange_rate(from_currency, to_currency, transaction_date)
		if rate is not None:
			rate_float = float(rate)
			if rate_float > 0:
				return rate_float
	except Exception:
		pass

	direct = _safe_get_all(
		"Currency Exchange",
		filters={"from_currency": from_currency, "to_currency": to_currency, "date": ["<=", transaction_date]},
		fields=["exchange_rate"],
		order_by="date desc",
		limit_page_length=1,
	)
	if direct:
		try:
			rate_float = float(direct[0].get("exchange_rate") or 0)
			if rate_float > 0:
				return rate_float
		except Exception:
			pass

	inverse = _safe_get_all(
		"Currency Exchange",
		filters={"from_currency": to_currency, "to_currency": from_currency, "date": ["<=", transaction_date]},
		fields=["exchange_rate"],
		order_by="date desc",
		limit_page_length=1,
	)
	if inverse:
		try:
			inverse_rate = float(inverse[0].get("exchange_rate") or 0)
			if inverse_rate > 0:
				return 1.0 / inverse_rate
		except Exception:
			pass

	return None


def _group_rows_by_parent(rows: list[dict[str, Any]], *, parent_key: str = "parent") -> dict[str, list[dict[str, Any]]]:
	grouped: dict[str, list[dict[str, Any]]] = {}
	for row in rows:
		parent = row.get(parent_key)
		if not parent:
			continue
		grouped.setdefault(parent, []).append(row)
	return grouped


def _sales_invoice_base_fields() -> list[str]:
	return [
		"name",
		"customer",
		"company",
		"customer_name",
		"posting_date",
		"due_date",
		"status",
		"debit_to",
		"currency",
		"conversion_rate",
		"base_grand_total",
		"base_total",
		"base_net_total",
		"base_total_taxes_and_charges",
		"base_rounding_adjustment",
		"base_rounded_total",
		"base_discount_amount",
		"base_paid_amount",
		"base_change_amount",
		"base_write_off_amount",
		"base_outstanding_amount",
		"outstanding_amount",
		"grand_total",
		"rounded_total",
		"rounding_adjustment",
		"discount_amount",
		"disable_rounded_total",
		"paid_amount",
		"net_total",
		"total",
		"total_taxes_and_charges",
		"is_pos",
		"update_stock",
		"pos_profile",
		"docstatus",
		"contact_display",
		"contact_mobile",
		"party_account_currency",
		"is_return",
		"return_against",
		"payment_terms",
		"custom_payment_currency",
		"custom_exchange_rate",
		"posa_delivery_charges",
		"modified",
	]


def _attach_invoice_children(invoices: list[dict[str, Any]]) -> None:
	invoice_names = [row.get("name") for row in invoices if row.get("name")]
	if not invoice_names:
		return

	item_filters: dict[str, Any] = {"parent": ["in", invoice_names]}
	if "parenttype" in _get_doctype_fieldnames("Sales Invoice Item"):
		item_filters["parenttype"] = "Sales Invoice"
	item_rows = _safe_get_all(
		"Sales Invoice Item",
		filters=item_filters,
		fields=[
			"parent",
			"item_code",
			"item_name",
			"description",
			"qty",
			"rate",
			"amount",
			"discount_percentage",
			"warehouse",
			"income_account",
			"sales_order",
			"so_detail",
			"delivery_note",
			"dn_detail",
			"cost_center",
		],
		order_by="idx asc",
		page_length=0,
	)
	items_by_invoice = _group_rows_by_parent(item_rows)

	payment_filters: dict[str, Any] = {"parent": ["in", invoice_names]}
	if "parenttype" in _get_doctype_fieldnames("Sales Invoice Payment"):
		payment_filters["parenttype"] = "Sales Invoice"
	payment_rows = _safe_get_all(
		"Sales Invoice Payment",
		filters=payment_filters,
		fields=["parent", "mode_of_payment", "amount", "account", "payment_reference", "type"],
		order_by="idx asc",
		page_length=0,
	)
	payments_by_invoice = _group_rows_by_parent(payment_rows)

	schedule_filters: dict[str, Any] = {"parent": ["in", invoice_names]}
	if "parenttype" in _get_doctype_fieldnames("Payment Schedule"):
		schedule_filters["parenttype"] = "Sales Invoice"
	schedule_rows = _safe_get_all(
		"Payment Schedule",
		filters=schedule_filters,
		fields=["parent", "payment_term", "invoice_portion", "due_date", "mode_of_payment"],
		order_by="idx asc",
		page_length=0,
	)
	schedule_by_invoice = _group_rows_by_parent(schedule_rows)

	for invoice in invoices:
		name = invoice.get("name")
		invoice["items"] = items_by_invoice.get(name, [])
		invoice["payments"] = payments_by_invoice.get(name, [])
		invoice["payment_schedule"] = schedule_by_invoice.get(name, [])


def _as_float(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except Exception:
		return default


def _as_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except Exception:
		return default


def _normalize_sales_invoice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	for row in rows:
		customer = str(row.get("customer") or "").strip()
		row["customer"] = customer
		row["customer_name"] = str(row.get("customer_name") or customer).strip()
		row["company"] = str(row.get("company") or "").strip()
		row["posting_date"] = str(row.get("posting_date") or nowdate())
		row["net_total"] = _as_float(row.get("net_total"))
		row["total"] = _as_float(row.get("total"))
		row["grand_total"] = _as_float(row.get("grand_total"))
		row["total_taxes_and_charges"] = _as_float(row.get("total_taxes_and_charges"))
		row["base_grand_total"] = _as_float(row.get("base_grand_total"))
		row["base_total"] = _as_float(row.get("base_total"))
		row["base_net_total"] = _as_float(row.get("base_net_total"))
		row["base_total_taxes_and_charges"] = _as_float(row.get("base_total_taxes_and_charges"))
		row["base_rounding_adjustment"] = _as_float(row.get("base_rounding_adjustment"))
		row["base_rounded_total"] = _as_float(row.get("base_rounded_total"))
		row["base_discount_amount"] = _as_float(row.get("base_discount_amount"))
		row["base_paid_amount"] = _as_float(row.get("base_paid_amount"))
		row["base_change_amount"] = _as_float(row.get("base_change_amount"))
		row["base_write_off_amount"] = _as_float(row.get("base_write_off_amount"))
		row["base_outstanding_amount"] = _as_float(row.get("base_outstanding_amount"))
		row["discount_amount"] = _as_float(row.get("discount_amount"))
		row["paid_amount"] = _as_float(row.get("paid_amount"))
		row["change_amount"] = _as_float(row.get("change_amount"))
		row["write_off_amount"] = _as_float(row.get("write_off_amount"))
		row["outstanding_amount"] = _as_float(row.get("outstanding_amount"))
		row["rounded_total"] = _as_float(row.get("rounded_total"))
		row["rounding_adjustment"] = _as_float(row.get("rounding_adjustment"))
		row["conversion_rate"] = _as_float(row.get("conversion_rate") or 1)
		row["custom_exchange_rate"] = _as_float(row.get("custom_exchange_rate"))
		row["is_pos"] = 1 if to_bool(row.get("is_pos"), default=False) else 0
		row["update_stock"] = 1 if to_bool(row.get("update_stock"), default=False) else 0
		row["disable_rounded_total"] = 1 if to_bool(row.get("disable_rounded_total"), default=False) else 0
		row["is_return"] = _as_int(row.get("is_return"))
		row["docstatus"] = _as_int(row.get("docstatus"))
		row["currency"] = str(row.get("currency") or "").strip() or None
		row["party_account_currency"] = str(row.get("party_account_currency") or "").strip() or None
		row["payment_terms"] = str(row.get("payment_terms") or "").strip() or None
		row["custom_payment_currency"] = str(row.get("custom_payment_currency") or "").strip() or None
		row["posa_delivery_charges"] = str(row.get("posa_delivery_charges") or "").strip() or None

		items: list[dict[str, Any]] = []
		for item in row.get("items") or []:
			item_code = str(item.get("item_code") or "").strip()
			if not item_code:
				continue
			items.append(
				{
					"item_code": item_code,
					"item_name": str(item.get("item_name") or item_code),
					"description": str(item.get("description") or ""),
					"qty": _as_float(item.get("qty")),
					"rate": _as_float(item.get("rate")),
					"amount": _as_float(item.get("amount")),
					"discount_percentage": _as_float(item.get("discount_percentage")),
					"warehouse": item.get("warehouse"),
					"income_account": item.get("income_account"),
					"sales_order": item.get("sales_order"),
					"so_detail": item.get("so_detail"),
					"delivery_note": item.get("delivery_note"),
					"dn_detail": item.get("dn_detail"),
					"cost_center": item.get("cost_center"),
				}
			)
		row["items"] = items

		payments: list[dict[str, Any]] = []
		for payment in row.get("payments") or []:
			mode = str(payment.get("mode_of_payment") or "").strip()
			if not mode:
				continue
			payments.append(
				{
					"mode_of_payment": mode,
					"amount": _as_float(payment.get("amount")),
					"account": payment.get("account"),
					"payment_reference": payment.get("payment_reference"),
					"type": payment.get("type") or "Receive",
				}
			)
		row["payments"] = payments

		schedule: list[dict[str, Any]] = []
		for due in row.get("payment_schedule") or []:
			schedule.append(
				{
					"payment_term": due.get("payment_term"),
					"invoice_portion": _as_float(due.get("invoice_portion")),
					"due_date": due.get("due_date"),
					"mode_of_payment": due.get("mode_of_payment"),
				}
			)
		row["payment_schedule"] = schedule
	return rows


def _payment_entry_base_fields() -> list[str]:
	return [
		"name",
		"posting_date",
		"party",
		"party_type",
		"payment_type",
		"mode_of_payment",
		"paid_amount",
		"received_amount",
		"paid_from_account_currency",
		"paid_to_account_currency",
		"docstatus",
		"modified",
	]


def _attach_payment_entry_references(entries: list[dict[str, Any]]) -> None:
	entry_names = [row.get("name") for row in entries if row.get("name")]
	if not entry_names:
		return

	reference_filters: dict[str, Any] = {"parent": ["in", entry_names]}
	if "parenttype" in _get_doctype_fieldnames("Payment Entry Reference"):
		reference_filters["parenttype"] = "Payment Entry"
	reference_rows = _safe_get_all(
		"Payment Entry Reference",
		filters=reference_filters,
		fields=["parent", "reference_doctype", "reference_name", "outstanding_amount", "allocated_amount"],
		order_by="idx asc",
		page_length=0,
	)
	references_by_entry = _group_rows_by_parent(reference_rows)

	for entry in entries:
		entry["references"] = references_by_entry.get(entry.get("name"), [])


def _normalize_payment_entry_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	for row in rows:
		row["paid_amount"] = _as_float(row.get("paid_amount"))
		row["received_amount"] = _as_float(row.get("received_amount"))
		row["posting_date"] = str(row.get("posting_date") or nowdate())
		row["docstatus"] = _as_int(row.get("docstatus"))
		references: list[dict[str, Any]] = []
		for ref in row.get("references") or []:
			references.append(
				{
					"reference_doctype": ref.get("reference_doctype"),
					"reference_name": ref.get("reference_name"),
					"outstanding_amount": _as_float(ref.get("outstanding_amount")),
					"allocated_amount": _as_float(ref.get("allocated_amount")),
				}
			)
		row["references"] = references
	return rows


def _get_customers(
	route: str | None,
	territory: str | None,
	*,
	modified_since: str | None = None,
	include_disabled: bool = False,
) -> list[dict[str, Any]]:
	customer_fields = _get_doctype_fieldnames("Customer")
	filters: dict[str, Any] = {}
	if not include_disabled:
		filters["disabled"] = 0
	if modified_since:
		filters["modified"] = [">=", modified_since]
	if route and "route" in customer_fields:
		filters["route"] = route
	elif territory and "territory" in customer_fields:
		filters["territory"] = territory

	selected_fields = [
		"name",
		"customer_name",
		"route",
		"territory",
		"mobile_no",
		"primary_address",
		"email_id",
		"image",
		"customer_type",
		"disabled",
	]
	customers = _safe_get_all(
		"Customer",
		filters=filters,
		fields=selected_fields,
		order_by="customer_name asc",
		page_length=0,
	)
	for customer in customers:
		customer.setdefault("route", None)
		customer.setdefault("territory", None)
		customer["customer_type"] = customer.get("customer_type") or "Individual"
		customer["disabled"] = 1 if to_bool(customer.get("disabled"), default=False) else 0
	customer_names = [row.get("name") for row in customers if row.get("name")]
	credit_rows = (
		_safe_get_all(
			"Customer Credit Limit",
			filters={"parent": ["in", customer_names]},
			fields=["parent", "company", "credit_limit", "bypass_credit_limit_check"],
			page_length=0,
		)
		if customer_names
		else []
	)
	credit_by_parent: dict[str, list[dict[str, Any]]] = {}
	for row in credit_rows:
		parent = row.get("parent")
		if not parent:
			continue
		credit_by_parent.setdefault(parent, []).append(
			{
				"company": row.get("company"),
				"credit_limit": row.get("credit_limit"),
				"bypass_credit_limit_check": row.get("bypass_credit_limit_check"),
			}
		)
	for customer in customers:
		customer["credit_limits"] = credit_by_parent.get(customer.get("name"), [])
	return customers


def _get_sales_invoices_delta(*, modified_since: str, profile_name: str | None) -> list[dict[str, Any]]:
	filters: dict[str, Any] = {"modified": [">=", modified_since]}
	if profile_name:
		filters["pos_profile"] = profile_name

	base_fields = _sales_invoice_base_fields()
	query_fields = _get_queryable_fields("Sales Invoice", base_fields)
	invoices = _safe_get_all(
		"Sales Invoice",
		filters=filters,
		fields=query_fields,
		order_by="modified asc",
		page_length=0,
	)
	for row in invoices:
		for fieldname in base_fields:
			row.setdefault(fieldname, None)
		row.setdefault("items", [])
		row.setdefault("payments", [])
		row.setdefault("payment_schedule", [])
	_attach_invoice_children(invoices)
	return _normalize_sales_invoice_rows(invoices)


def _get_payment_entries_delta(*, modified_since: str) -> list[dict[str, Any]]:
	base_fields = _payment_entry_base_fields()
	query_fields = _get_queryable_fields("Payment Entry", base_fields)
	entries = _safe_get_all(
		"Payment Entry",
		filters={"modified": [">=", modified_since], "party_type": "Customer", "payment_type": "Receive"},
		fields=query_fields,
		order_by="modified asc",
		page_length=0,
	)
	for row in entries:
		for fieldname in base_fields:
			row.setdefault(fieldname, None)
		row.setdefault("references", [])
	_attach_payment_entry_references(entries)
	return _normalize_payment_entry_rows(entries)


def _get_inventory_delta(
	*,
	modified_since: str,
	warehouse: str | None,
	price_list: str | None,
) -> list[dict[str, Any]]:
	if not warehouse:
		return []

	item_codes: set[str] = set()
	for row in _safe_get_all(
		"Bin",
		filters={"warehouse": warehouse, "modified": [">=", modified_since]},
		fields=["item_code"],
		page_length=0,
	):
		item_code = (row.get("item_code") or "").strip()
		if item_code:
			item_codes.add(item_code)

	for row in _safe_get_all(
		"Item",
		filters={"modified": [">=", modified_since]},
		fields=["name as item_code"],
		page_length=0,
	):
		item_code = (row.get("item_code") or "").strip()
		if item_code:
			item_codes.add(item_code)

	price_filters: dict[str, Any] = {"modified": [">=", modified_since], "selling": 1}
	if price_list:
		price_filters["price_list"] = price_list
	for row in _safe_get_all(
		"Item Price",
		filters=price_filters,
		fields=["item_code"],
		page_length=0,
	):
		item_code = (row.get("item_code") or "").strip()
		if item_code:
			item_codes.add(item_code)

	if not item_codes:
		return []

	items = _build_inventory_items_for_item_codes(
		warehouse=warehouse,
		price_list=price_list or "",
		item_codes=sorted(item_codes),
	)
	if not items:
		return []
	alerts = _build_inventory_alerts(warehouse=warehouse, items=items) if _has_negative_inventory_rows(items) else []
	return _apply_inventory_visibility_rules(items=items, alerts=alerts)


def _has_negative_inventory_rows(items: list[dict[str, Any]]) -> bool:
	for row in items:
		raw_qty_value = row.get("_raw_actual_qty")
		if raw_qty_value is None:
			raw_qty_value = row.get("actual_qty")
		try:
			raw_qty = float(raw_qty_value or 0)
		except (TypeError, ValueError):
			raw_qty = 0.0
		if raw_qty < 0:
			return True
	return False


def _get_item_barcodes(item_codes: list[str]) -> dict[str, str]:
	if not item_codes:
		return {}
	filters: dict[str, Any] = {"parent": ["in", item_codes]}
	if "parenttype" in _get_doctype_fieldnames("Item Barcode"):
		filters["parenttype"] = "Item"
	rows = _safe_get_all(
		"Item Barcode",
		filters=filters,
		fields=["parent", "barcode"],
		order_by="idx asc",
		page_length=0,
	)
	barcode_by_item: dict[str, str] = {}
	for row in rows:
		parent = row.get("parent")
		barcode = (row.get("barcode") or "").strip()
		if parent and barcode and parent not in barcode_by_item:
			barcode_by_item[parent] = barcode
	return barcode_by_item


def _get_item_variant_descriptors(item_codes: list[str]) -> dict[str, str]:
	if not item_codes:
		return {}
	filters: dict[str, Any] = {"parent": ["in", item_codes]}
	if "parenttype" in _get_doctype_fieldnames("Item Variant Attribute"):
		filters["parenttype"] = "Item"
	rows = _safe_get_all(
		"Item Variant Attribute",
		filters=filters,
		fields=["parent", "attribute", "attribute_value"],
		order_by="idx asc",
		page_length=0,
	)
	descriptor_map: dict[str, list[str]] = {}
	for row in rows:
		parent = row.get("parent")
		if not parent:
			continue
		attribute = (row.get("attribute") or "").strip()
		value = (row.get("attribute_value") or "").strip()
		if not value:
			continue
		text = f"{attribute}: {value}" if attribute else value
		descriptor_map.setdefault(parent, []).append(text)
	return {item_code: ", ".join(values) for item_code, values in descriptor_map.items() if values}


def _build_inventory_items_for_item_codes(
	*,
	warehouse: str,
	price_list: str,
	item_codes: list[str],
) -> list[dict[str, Any]]:
	if not item_codes:
		return []

	bins = _safe_get_all(
		"Bin",
		filters={"warehouse": warehouse, "item_code": ["in", item_codes]},
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "projected_qty", "stock_uom", "valuation_rate"],
		page_length=0,
	)
	bin_by_code = {row.get("item_code"): row for row in bins if row.get("item_code")}

	items = _safe_get_all(
		"Item",
		filters={"name": ["in", item_codes]},
		fields=[
			"item_code",
			"item_name",
			"item_group",
			"description",
			"brand",
			"image",
			"stock_uom",
			"standard_rate",
			"is_stock_item",
			"is_sales_item",
			"variant_of",
			"disabled",
		],
		page_length=0,
	)
	item_by_code = {row.get("item_code"): row for row in items if row.get("item_code")}
	barcode_by_code = _get_item_barcodes(item_codes)
	variant_descriptors = _get_item_variant_descriptors(item_codes)

	price_filters: dict[str, Any] = {"item_code": ["in", item_codes], "selling": 1}
	if price_list:
		price_filters["price_list"] = price_list
	prices = _safe_get_all(
		"Item Price",
		filters=price_filters,
		fields=["item_code", "price_list", "price_list_rate", "currency"],
		page_length=0,
		order_by="modified desc",
	)
	price_by_code: dict[str, dict[str, Any]] = {}
	for row in prices:
		item_code = row.get("item_code")
		if item_code:
			price_by_code.setdefault(item_code, row)

	output: list[dict[str, Any]] = []
	for item_code in item_codes:
		item = item_by_code.get(item_code)
		if not item:
			continue

		item_code_text = str(item_code or "").strip()
		item_path_segment = quote(item_code_text, safe="")
		desk_route = f"/desk/item/{item_path_segment}"
		desk_url = f"{get_url()}{desk_route}#details"
		bin_row = bin_by_code.get(item_code) or {}
		price_row = price_by_code.get(item_code)
		actual_qty = float(bin_row.get("actual_qty") or 0)
		reserved_qty = float(bin_row.get("reserved_qty") or 0)
		sellable_qty = max(actual_qty - reserved_qty, 0)
		price = (price_row.get("price_list_rate") if price_row else item.get("standard_rate")) or 0
		currency = price_row.get("currency") if price_row else ""
		is_stocked = bool(item.get("is_stock_item"))
		is_service = (not is_stocked) or (item.get("item_group") == "COMPLEMENTARIOS")
		variant_description = (variant_descriptors.get(item_code) or "").strip()
		item_name = item.get("item_name") or item_code
		if variant_description and variant_description.lower() not in str(item_name).lower():
			item_name = f"{item_name} ({variant_description})"
		output.append(
			{
				"item_code": item_code,
				"actual_qty": sellable_qty,
				"_raw_actual_qty": actual_qty,
				"price": price,
				"valuation_rate": bin_row.get("valuation_rate") or 0,
				"name": item_name,
				"item_group": item.get("item_group") or "",
				"description": item.get("description") or "",
				"barcode": barcode_by_code.get(item_code, ""),
				"image": item.get("image") or "",
				"discount": 0.0,
				"is_service": 1 if is_service else 0,
				"is_stocked": 1 if is_stocked else 0,
				"stock_uom": item.get("stock_uom") or bin_row.get("stock_uom") or "",
				"brand": item.get("brand") or "",
				"currency": currency or "",
				"projected_qty": bin_row.get("projected_qty") or actual_qty,
				"variant_of": item.get("variant_of") or None,
				"variant_attributes": variant_description or None,
				"desk_route": desk_route,
				"desk_url": desk_url,
			}
		)
	return output


def _get_invoices(profile_name: str, settings, *, recent_paid_only: bool) -> list[dict[str, Any]]:
	if not profile_name:
		return []

	invoice_days = int(settings.bootstrap_invoice_days or 90)
	start_date = add_days(nowdate(), -invoice_days)
	base_fields = _sales_invoice_base_fields()
	open_statuses = [
		"Draft",
		"Unpaid",
		"Overdue",
		"Partly Paid",
		"Overdue and Discounted",
		"Unpaid and Discounted",
		"Partly Paid and Discounted",
		"Cancelled",
		"Credit Note Issued",
		"Return",
	]
	query_fields = _get_queryable_fields("Sales Invoice", base_fields)
	invoices = _safe_get_all(
		"Sales Invoice",
		filters={"pos_profile": profile_name, "posting_date": [">=", start_date], "status": ["in", open_statuses]},
		fields=query_fields,
		order_by="posting_date desc",
		page_length=0,
	)
	for row in invoices:
		for fieldname in base_fields:
			row.setdefault(fieldname, None)
	if recent_paid_only:
		paid_days = int(settings.recent_paid_invoice_days or 7)
		paid_start = add_days(nowdate(), -paid_days)
		paid = _safe_get_all(
			"Sales Invoice",
			filters={"pos_profile": profile_name, "posting_date": [">=", paid_start], "status": "Paid"},
			fields=query_fields,
			order_by="posting_date desc",
			page_length=0,
		)
		seen = {row.get("name") for row in invoices if row.get("name")}
		for row in paid:
			for fieldname in base_fields:
				row.setdefault(fieldname, None)
			if row.get("name") not in seen:
				invoices.append(row)
	for row in invoices:
		row.setdefault("items", [])
		row.setdefault("payments", [])
		row.setdefault("payment_schedule", [])
	_attach_invoice_children(invoices)
	return _normalize_sales_invoice_rows(invoices)


def _get_payment_entries(from_date: str) -> list[dict[str, Any]]:
	base_fields = _payment_entry_base_fields()
	rows = _safe_get_all(
		"Payment Entry",
		filters={
			"posting_date": [">=", from_date],
			"docstatus": 1,
			"party_type": "Customer",
			"payment_type": "Receive",
		},
		fields=base_fields,
		order_by="posting_date desc",
		page_length=0,
	)
	for row in rows:
		for fieldname in base_fields:
			row.setdefault(fieldname, None)
		row.setdefault("references", [])
	_attach_payment_entry_references(rows)
	return _normalize_payment_entry_rows(rows)
