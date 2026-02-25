from typing import Any

import frappe
from frappe.utils.data import add_days, nowdate
from .common import ok, parse_payload, standard_api_response, to_bool, value_from_aliases
from .inventory import _apply_inventory_visibility_rules, _build_inventory_alerts, \
	_build_inventory_items
from .shipping_rule import get_shipping_rules


def _get_doctype_fieldnames(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	return set(frappe.get_all("DocField", filters={"parent": doctype}, pluck="fieldname", page_length=0))


def _build_pagination(offset: int, limit: int, total: int, count: int) -> dict[str, Any]:
	offset_value = max(int(offset or 0), 0)
	limit_value = max(int(limit or 0), 0)
	total_value = max(int(total or 0), 0)
	has_more = False
	if limit_value > 0:
		has_more = offset_value + count < total_value
	return {
		"offset": offset_value,
		"limit": limit_value,
		"total": total_value,
		"has_more": 1 if has_more else 0,
	}


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


def _get_open_shift(profile_name: str | None, opening_name: str | None) -> dict[str, Any] | None:
	"""Return the active POS Opening Entry for the current user (if any)."""
	if not frappe.db.exists("DocType", "POS Opening Entry"):
		return None

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
	rows = frappe.get_all(
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
		open_entry["balance_details"] = _get_opening_balance_details(str(open_entry.get("name") or ""))
		return open_entry

	return None


def _get_opening_balance_details(opening_name: str) -> list[dict[str, Any]]:
	"""Return opening amounts per payment mode for a POS Opening Entry."""
	opening_name = str(opening_name or "").strip()
	if not opening_name or not frappe.db.exists("DocType", "POS Opening Entry Detail"):
		return []

	filters: dict[str, Any] = {"parent": opening_name}
	detail_fields = _get_doctype_fieldnames("POS Opening Entry Detail")
	if "parenttype" in detail_fields:
		filters["parenttype"] = "POS Opening Entry"

	rows = frappe.get_all(
		"POS Opening Entry Detail",
		filters=filters,
		fields=["mode_of_payment", "opening_amount"],
		order_by="idx asc",
		page_length=0,
	)

	output: list[dict[str, Any]] = []
	for row in rows:
		mode = str(row.get("mode_of_payment") or "").strip()
		if not mode:
			continue
		try:
			opening_amount = float(row.get("opening_amount") or 0)
		except Exception:
			opening_amount = 0.0
		output.append({"mode_of_payment": mode, "opening_amount": opening_amount})
	return output


def _get_pos_closing_entry_details(closing_name: str) -> list[dict[str, Any]]:
	if not closing_name or not frappe.db.exists("DocType", "POS Closing Entry Detail"):
		return []
	filters: dict[str, Any] = {"parent": closing_name}
	detail_fields = _get_doctype_fieldnames("POS Closing Entry Detail")
	if "parenttype" in detail_fields:
		filters["parenttype"] = "POS Closing Entry"
	rows = frappe.get_all(
		"POS Closing Entry Detail",
		filters=filters,
		fields=["mode_of_payment", "opening_amount", "expected_amount", "closing_amount", "difference"],
		order_by="idx asc",
		page_length=0,
	)
	return [
		{
			"mode_of_payment": str(row.get("mode_of_payment") or "").strip(),
			"opening_amount": _as_float(row.get("opening_amount")),
			"expected_amount": _as_float(row.get("expected_amount")),
			"closing_amount": _as_float(row.get("closing_amount")),
			"difference": _as_float(row.get("difference")),
		}
		for row in rows
		if row.get("mode_of_payment")
	]


def _get_latest_pos_closing_entry(
	*,
	user: str | None,
	profile_name: str | None,
	opening_name: str | None,
) -> dict[str, Any] | None:
	if not frappe.db.exists("DocType", "POS Closing Entry"):
		return None

	fields = _get_doctype_fieldnames("POS Closing Entry")
	query_fields = [
		"name",
		"status",
		"pos_profile",
		"company",
		"user",
		"posting_date",
		"posting_time",
		"period_start_date",
		"period_end_date",
		"pos_opening_entry",
		"modified",
	]
	query_fields = [f for f in query_fields if f in fields or f == "name"] or ["name"]

	filters: dict[str, Any] = {}
	if user and "user" in fields:
		filters["user"] = user
	if profile_name and "pos_profile" in fields:
		filters["pos_profile"] = profile_name
	if opening_name and "pos_opening_entry" in fields:
		filters["pos_opening_entry"] = opening_name

	rows = frappe.get_all(
		"POS Closing Entry",
		filters=filters,
		fields=query_fields + (["docstatus"] if "docstatus" in fields else []),
		order_by="modified desc",
		page_length=5,
	)
	if not rows:
		return None
	selected = None
	for row in rows:
		if int(row.get("docstatus") or 0) == 1:
			selected = row
			break
	entry = dict(selected or rows[0])
	if entry.get("name") and "id" not in entry:
		entry["id"] = entry.get("name")
	entry["payment_reconciliation"] = _get_pos_closing_entry_details(str(entry.get("name") or ""))
	return entry


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def my_pos_profiles(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
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
def pos_profile_detail(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	"""Return a single accessible POS Profile detail (with payment methods metadata)."""
	body = parse_payload(payload)
	profile_name = str(
		value_from_aliases(
			body,
			"profile_name",
			"profileName",
			"pos_profile",
			"posProfile",
			"name",
			default="",
		)
		or ""
	).strip()
	if not profile_name:
		frappe.throw("profile_name is required")

	profiles = _get_accessible_pos_profiles(frappe.session.user)
	accessible_profile_names = {str(row.get("name") or "").strip() for row in profiles if str(row.get("name") or "").strip()}
	if profile_name not in accessible_profile_names:
		frappe.throw(f"User {frappe.session.user} does not have access to POS Profile {profile_name}.")

	detail = _get_pos_profile_detail(profile_name)
	if not detail:
		frappe.throw(f"POS Profile {profile_name} not found.")

	return ok({"profile_name": profile_name, "pos_profile_detail": detail})


def _get_pos_profile_detail(profile_name: str) -> dict[str, Any] | None:
	if not profile_name:
		return None
	if not frappe.db.exists("POS Profile", profile_name):
		return None

	optional_profile_fields = [
		"warehouse",
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
	profile["profile_name"] = profile.get("name")
	profile.pop("profileName", None)
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
	mode_metadata = _get_pos_payment_mode_metadata(
		[str(row.get("mode_of_payment") or "").strip() for row in payments],
		company=str(profile.get("company") or "").strip() or None,
	)
	for payment in payments:
		for fieldname in payment_optional_fields:
			payment.setdefault(fieldname, 0 if fieldname in {"default", "allow_in_returns"} else "")
		payment["name"] = payment.get("name") or payment.get("mode_of_payment") or ""
		payment["mode_of_payment"] = payment.get("mode_of_payment") or ""
		payment["default"] = 1 if to_bool(payment.get("default"), default=False) else 0
		payment["allow_in_returns"] = 1 if to_bool(payment.get("allow_in_returns"), default=False) else 0
		mode_name = str(payment.get("mode_of_payment") or "").strip()
		meta = mode_metadata.get(mode_name, {})
		account = str(meta.get("account") or "").strip()
		payment["account"] = account or None
		payment["default_account"] = account or None
		payment["currency"] = str(meta.get("currency") or "").strip() or None
		payment["account_currency"] = str(meta.get("account_currency") or "").strip() or None
		payment["account_type"] = str(meta.get("account_type") or "").strip() or None
		payment["mode_of_payment_type"] = str(meta.get("type") or "").strip() or None
		payment["enabled"] = 1 if to_bool(meta.get("enabled"), default=True) else 0
		payment["company"] = str(meta.get("company") or profile.get("company") or "").strip() or None
		payment["accounts"] = list(meta.get("accounts") or [])
	profile["payments"] = payments
	return profile


def _get_pos_payment_mode_metadata(
	mode_names: list[str],
	*,
	company: str | None,
) -> dict[str, dict[str, Any]]:
	"""Resolve account and currency metadata for each Mode of Payment used in POS Profile."""
	normalized = sorted({str(name or "").strip() for name in mode_names if str(name or "").strip()})
	if not normalized or not frappe.db.exists("DocType", "Mode of Payment"):
		return {}

	mode_fieldnames = _get_doctype_fieldnames("Mode of Payment")
	mode_fields = ["name"]
	for fieldname in ("mode_of_payment", "enabled", "type"):
		if fieldname in mode_fieldnames:
			mode_fields.append(fieldname)

	mode_rows = frappe.get_all(
		"Mode of Payment",
		filters={"name": ["in", normalized]},
		fields=mode_fields,
		page_length=0,
	)

	metadata_by_mode: dict[str, dict[str, Any]] = {}
	mode_docnames: list[str] = []
	for row in mode_rows:
		docname = str(row.get("name") or "").strip()
		if not docname:
			continue
		mode_docnames.append(docname)
		display_name = str(row.get("mode_of_payment") or docname).strip() or docname
		meta = {
			"name": docname,
			"mode_of_payment": display_name,
			"enabled": 1 if to_bool(row.get("enabled"), default=True) else 0,
			"type": str(row.get("type") or "").strip() or None,
			"account": None,
			"default_account": None,
			"currency": None,
			"account_currency": None,
			"account_type": None,
			"company": company,
			"accounts": [],
		}
		metadata_by_mode[docname] = dict(meta)
		metadata_by_mode[display_name] = dict(meta)

	if not mode_docnames:
		return metadata_by_mode

	account_rows_by_mode: dict[str, list[dict[str, Any]]] = {}
	if frappe.db.exists("DocType", "Mode of Payment Account"):
		mopa_fieldnames = _get_doctype_fieldnames("Mode of Payment Account")
		mopa_filters: dict[str, Any] = {"parent": ["in", mode_docnames]}
		if "parenttype" in mopa_fieldnames:
			mopa_filters["parenttype"] = "Mode of Payment"
		mopa_fields = [field for field in ["parent", "company", "default_account"] if field in (mopa_fieldnames | {"parent"})]
		mopa_rows = frappe.get_all(
			"Mode of Payment Account",
			filters=mopa_filters,
			fields=mopa_fields or ["parent"],
			page_length=0,
		)
		for row in mopa_rows:
			parent = str(row.get("parent") or "").strip()
			if not parent:
				continue
			account_rows_by_mode.setdefault(parent, []).append(
				{
					"company": str(row.get("company") or "").strip() or None,
					"default_account": str(row.get("default_account") or "").strip() or None,
				}
			)

	selected_accounts: set[str] = set()
	for mode_key, meta in list(metadata_by_mode.items()):
		mode_docname = str(meta.get("name") or "").strip()
		if not mode_docname:
			continue
		accounts = list(account_rows_by_mode.get(mode_docname, []))
		selected = None
		if company:
			selected = next(
				(
					row for row in accounts
					if str(row.get("company") or "").strip() == company and str(row.get("default_account") or "").strip()
				),
				None,
			)
		if not selected:
			selected = next((row for row in accounts if str(row.get("default_account") or "").strip()), None)
		account_name = str((selected or {}).get("default_account") or "").strip()
		if account_name:
			selected_accounts.add(account_name)

		meta["accounts"] = accounts
		meta["account"] = account_name or None
		meta["default_account"] = account_name or None
		meta["company"] = str((selected or {}).get("company") or company or "").strip() or None
		metadata_by_mode[mode_key] = meta

	account_detail_by_name: dict[str, dict[str, Any]] = {}
	if selected_accounts and frappe.db.exists("DocType", "Account"):
		account_fieldnames = _get_doctype_fieldnames("Account")
		account_fields = ["name"] + [
			fieldname
			for fieldname in ("account_currency", "account_type", "company")
			if fieldname in account_fieldnames
		]
		account_rows = frappe.get_all(
			"Account",
			filters={"name": ["in", sorted(selected_accounts)]},
			fields=account_fields,
			page_length=0,
		)
		account_detail_by_name = {
			str(row.get("name") or "").strip(): row
			for row in account_rows
			if str(row.get("name") or "").strip()
		}

	for mode_key, meta in list(metadata_by_mode.items()):
		account_name = str(meta.get("account") or "").strip()
		account_row = account_detail_by_name.get(account_name, {})
		account_currency = str(account_row.get("account_currency") or "").strip()
		account_type = str(account_row.get("account_type") or "").strip()
		meta["account_currency"] = account_currency or None
		meta["currency"] = account_currency or None
		meta["account_type"] = account_type or None
		if not meta.get("company"):
			meta_company = str(account_row.get("company") or "").strip()
			meta["company"] = meta_company or None
		metadata_by_mode[mode_key] = meta

	return metadata_by_mode


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

			rows = frappe.get_all(
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
	rows = frappe.get_all(
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
	currencies = frappe.get_all(
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

	direct = frappe.get_all(
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

	inverse = frappe.get_all(
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


def _normalize_inventory_alert(alert_row: dict[str, Any]) -> dict[str, Any]:
	item_code = str((alert_row.get("item_code") or alert_row.get("itemCode") or "")).strip()
	item_name = str((alert_row.get("item_name") or alert_row.get("itemName") or item_code)).strip()
	status = str(alert_row.get("status") or "").strip().upper()
	qty_value = alert_row.get("qty")
	reorder_level_value = alert_row.get("reorder_level")
	if reorder_level_value is None:
		reorder_level_value = alert_row.get("reorderLevel")
	reorder_qty_value = alert_row.get("reorder_qty")
	if reorder_qty_value is None:
		reorder_qty_value = alert_row.get("reorderQty")

	return {
		"item_code": item_code,
		"item_name": item_name,
		"status": status or None,
		"qty": _as_float(qty_value),
		"reorder_level": _as_float(reorder_level_value) if reorder_level_value is not None else None,
		"reorder_qty": _as_float(reorder_qty_value) if reorder_qty_value is not None else None,
		"itemCode": item_code,
		"itemName": item_name,
		"reorderLevel": _as_float(reorder_level_value) if reorder_level_value is not None else None,
		"reorderQty": _as_float(reorder_qty_value) if reorder_qty_value is not None else None,
	}


def _attach_alerts_to_inventory_items(
	items: list[dict[str, Any]],
	alerts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	alerts_by_item: dict[str, dict[str, Any]] = {}
	for alert in alerts or []:
		normalized = _normalize_inventory_alert(alert)
		item_code = normalized.get("item_code")
		if item_code:
			alerts_by_item[item_code] = normalized

	for row in items:
		item_code = str(row.get("item_code") or "").strip()
		alert = alerts_by_item.get(item_code)
		row["has_stock_alert"] = 1 if alert else 0
		row["stock_alert_status"] = (alert or {}).get("status")
		row["stock_alert_qty"] = (alert or {}).get("qty")
		row["stock_alert_reorder_level"] = (alert or {}).get("reorder_level")
		row["stock_alert_reorder_qty"] = (alert or {}).get("reorder_qty")
		row["stock_alert"] = alert

	return items


def _build_dummy_inventory_alerts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Return dummy alerts with real contract for app testing."""
	seed_rows = [
		{"item_code": "DUMMY-ALERT-001", "item_name": "Dummy Item 1", "qty": 2.0},
		{"item_code": "DUMMY-ALERT-002", "item_name": "Dummy Item 2", "qty": 0.0},
		{"item_code": "DUMMY-ALERT-003", "item_name": "Dummy Item 3", "qty": 5.0},
	]
	if items:
		seed_rows = []
		for row in items[:3]:
			code = str(row.get("item_code") or row.get("itemCode") or "DUMMY-ALERT").strip()
			name = str(row.get("name") or row.get("item_name") or row.get("itemName") or code).strip()
			qty = float(row.get("projected_qty") or row.get("actual_qty") or 0)
			seed_rows.append({"item_code": code, "item_name": name, "qty": qty})

	alerts: list[dict[str, Any]] = []
	for idx, row in enumerate(seed_rows, start=1):
		item_code = row.get("item_code")
		item_name = row.get("item_name") or item_code
		qty = float(row.get("qty") or 0)
		status = "CRITICAL" if qty <= 0 else "LOW"
		reorder_level = 10.0
		reorder_qty = 20.0
		alerts.append(
			{
				"itemCode": item_code,
				"item_code": item_code,
				"itemName": item_name,
				"item_name": item_name,
				"status": status,
				"qty": qty,
				"reorder_level": reorder_level,
				"reorder_qty": reorder_qty,
				"reorderLevel": reorder_level,
				"reorderQty": reorder_qty,
			}
		)
	return alerts


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
		"payment_terms_template",
		"modified",
	]


def _attach_invoice_children(invoices: list[dict[str, Any]]) -> None:
	invoice_names = [row.get("name") for row in invoices if row.get("name")]
	if not invoice_names:
		return

	item_filters: dict[str, Any] = {"parent": ["in", invoice_names]}
	if "parenttype" in _get_doctype_fieldnames("Sales Invoice Item"):
		item_filters["parenttype"] = "Sales Invoice"
	item_rows = frappe.get_all(
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

	payment_filters: dict[str, Any] = {'parent': ['in', invoice_names]}
	if 'parenttype' in _get_doctype_fieldnames('Sales Invoice Payment'):
		payment_filters['parenttype'] = 'Sales Invoice'
	payment_rows = frappe.get_all(
		"Sales Invoice Payment",
		filters=payment_filters,
		fields=["parent", "mode_of_payment", "amount", "account", "reference_no", "type"],
		order_by="idx asc",
		page_length=0,
	)
	payments_by_invoice = _group_rows_by_parent(payment_rows)

	schedule_filters: dict[str, Any] = {'parent': ['in', invoice_names]}
	if "parenttype" in _get_doctype_fieldnames('Payment Schedule'):
		schedule_filters['parenttype'] = 'Sales Invoice'
	schedule_rows = frappe.get_all(
		'Payment Schedule',
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
		row["discount_amount"] = _as_float(row.get("discount_amount"))
		row["paid_amount"] = _as_float(row.get("paid_amount"))
		row["change_amount"] = _as_float(row.get("change_amount"))
		row["write_off_amount"] = _as_float(row.get("write_off_amount"))
		row["outstanding_amount"] = _as_float(row.get("outstanding_amount"))
		row["rounded_total"] = _as_float(row.get("rounded_total"))
		row["rounding_adjustment"] = _as_float(row.get("rounding_adjustment"))
		row["conversion_rate"] = _as_float(row.get("conversion_rate") or 1)
		row["is_pos"] = 1 if to_bool(row.get("is_pos"), default=False) else 0
		row["update_stock"] = 1 if to_bool(row.get("update_stock"), default=False) else 0
		row["disable_rounded_total"] = 1 if to_bool(row.get("disable_rounded_total"), default=False) else 0
		row["is_return"] = _as_int(row.get("is_return"))
		row["docstatus"] = _as_int(row.get("docstatus"))
		row["currency"] = str(row.get("currency") or "").strip() or None
		row["party_account_currency"] = str(row.get("party_account_currency") or "").strip() or None

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


def _invoice_matches_profile(row: dict[str, Any], profile_name: str | None) -> bool:
	if not profile_name:
		return True
	row_profile = str(row.get("pos_profile") or "").strip()
	return (not row_profile) or row_profile == profile_name


def _payment_entry_base_fields() -> list[str]:
	fields = [
		"name",
		"posting_date",
		"company",
		"party",
		"party_type",
		"payment_type",
		"mode_of_payment",
		"paid_amount",
		"received_amount",
		"unallocated_amount",
		"paid_from_account_currency",
		"paid_to_account_currency",
		"docstatus",
		"modified",
	]
	if "territory" in _get_doctype_fieldnames("Payment Entry"):
		fields.insert(3, "territory")
	return fields


def _attach_payment_entry_references(entries: list[dict[str, Any]]) -> None:
	entry_names = [row.get("name") for row in entries if row.get("name")]
	if not entry_names:
		return

	reference_filters: dict[str, Any] = {"parent": ["in", entry_names]}
	if "parenttype" in _get_doctype_fieldnames("Payment Entry Reference"):
		reference_filters["parenttype"] = "Payment Entry"
	reference_rows = frappe.get_all(
		"Payment Entry Reference",
		filters=reference_filters,
		fields=[
			"parent",
			"reference_doctype",
			"reference_name",
			"total_amount",
			"outstanding_amount",
			"allocated_amount",
		],
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
		row["unallocated_amount"] = _as_float(row.get("unallocated_amount"))
		row["posting_date"] = str(row.get("posting_date") or nowdate())
		row["company"] = str(row.get("company") or "").strip()
		row["territory"] = str(row.get("territory") or "").strip() or None
		row["mode_of_payment"] = str(row.get("mode_of_payment") or "").strip()
		row["docstatus"] = _as_int(row.get("docstatus"))
		references: list[dict[str, Any]] = []
		for ref in row.get("references") or []:
			references.append(
				{
					"payment_entry": row.get("name"),
					"reference_doctype": ref.get("reference_doctype"),
					"reference_name": ref.get("reference_name"),
					"total_amount": _as_float(ref.get("total_amount")),
					"outstanding_amount": _as_float(ref.get("outstanding_amount")),
					"allocated_amount": _as_float(ref.get("allocated_amount")),
				}
			)
		row["references"] = references
	return rows


def _get_customers(
	territory: str | None,
	*,
	profile_name: str | None = None,
	company_name: str | None = None,
	modified_since: str | None = None,
	include_disabled: bool = False,
	offset: int = 0,
	limit: int = 0,
) -> list[dict[str, Any]]:
	customer_fields = _get_doctype_fieldnames("Customer")
	filters: dict[str, Any] = {}
	if not include_disabled:
		filters["disabled"] = 0
	if modified_since:
		filters["modified"] = [">=", modified_since]
	elif territory and "territory" in customer_fields:
		filters["territory"] = territory

	selected_fields = ["name", "customer_name"]
	for fieldname in (
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
	limit_value = max(int(limit or 0), 0)
	start_value = max(int(offset or 0), 0)
	customers = frappe.get_all(
		"Customer",
		filters=filters,
		fields=selected_fields,
		order_by="customer_name asc",
		page_length=0,
		limit_page_length=limit_value if limit_value > 0 else None,
		limit_start=start_value if limit_value > 0 else None,
	)
	for customer in customers:
		customer["territory"] = str(customer.get("territory") or "").strip()
		customer["customer_group"] = str(customer.get("customer_group") or "").strip() or None
		customer["default_currency"] = str(customer.get("default_currency") or "").strip() or None
		customer["default_price_list"] = str(customer.get("default_price_list") or "").strip() or None
		customer["customer_type"] = customer.get("customer_type") or "Individual"
		customer["disabled"] = 1 if to_bool(customer.get("disabled"), default=False) else 0
	customer_names = [row.get("name") for row in customers if row.get("name")]
	receivable_accounts_by_customer: dict[str, list[dict[str, Any]]] = {}
	if customer_names and frappe.db.exists("DocType", "Customer Account"):
		ca_fields = _get_doctype_fieldnames("Customer Account")
		ca_filters: dict[str, Any] = {"parent": ["in", customer_names]}
		if "parenttype" in ca_fields:
			ca_filters["parenttype"] = "Customer"
		ca_rows = frappe.get_all(
			"Customer Account",
			filters=ca_filters,
			fields=["parent", "company", "account"],
			page_length=0,
		)
		account_names = [row.get("account") for row in ca_rows if row.get("account")]
		account_currency_by_name = {}
		if account_names and frappe.db.exists("DocType", "Account"):
			account_currency_by_name = {
				row.get("name"): row.get("account_currency")
				for row in frappe.get_all(
					"Account",
					filters={"name": ["in", account_names]},
					fields=["name", "account_currency"],
					page_length=0,
				)
				if row.get("name")
			}
		for row in ca_rows:
			parent = row.get("parent")
			if not parent:
				continue
			account = row.get("account")
			entry = {
				"company": row.get("company"),
				"account": account,
				"account_currency": account_currency_by_name.get(account),
			}
			receivable_accounts_by_customer.setdefault(parent, []).append(entry)
	credit_rows = (
		frappe.get_all(
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

	supplier_accounts_by_customer: dict[str, list[dict[str, Any]]] = {}
	if customer_names and frappe.db.exists("DocType", "Supplier"):
		supplier_rows = frappe.get_all(
			"Supplier",
			filters={"name": ["in", customer_names]},
			fields=["name", "default_bank_account", "default_currency"],
			page_length=0,
		)
		bank_accounts: list[str] = []
		for row in supplier_rows:
			account = str(row.get("default_bank_account") or "").strip()
			if account:
				bank_accounts.append(account)
		bank_account_rows = []
		if bank_accounts and frappe.db.exists("DocType", "Bank Account"):
			bank_account_rows = frappe.get_all(
				"Bank Account",
				filters={"name": ["in", bank_accounts]},
				fields=["name", "account", "account_name", "bank", "company", "is_company_account"],
				page_length=0,
			)
		bank_account_by_name = {row.get("name"): row for row in bank_account_rows if row.get("name")}
		for row in supplier_rows:
			name = row.get("name")
			if not name:
				continue
			default_bank_account = str(row.get("default_bank_account") or "").strip()
			entry: dict[str, Any] = {
				"default_bank_account": default_bank_account or None,
				"default_currency": str(row.get("default_currency") or "").strip() or None,
			}
			bank_account = bank_account_by_name.get(default_bank_account, {}) if default_bank_account else {}
			if bank_account:
				entry.update(
					{
						"bank_account_name": bank_account.get("account_name") or None,
						"bank": bank_account.get("bank") or None,
						"company": bank_account.get("company") or None,
						"is_company_account": 1 if to_bool(bank_account.get("is_company_account"), default=False) else 0,
						"account": bank_account.get("account") or None,
					}
				)
			if entry.get("default_bank_account") or entry.get("account"):
				supplier_accounts_by_customer.setdefault(name, []).append(entry)

	outstanding_by_customer: dict[str, dict[str, float | int]] = {}
	if customer_names:
		outstanding_filters: dict[str, Any] = {
			"customer": ["in", customer_names],
			"status": [
				"in",
				[
					"Unpaid",
					"Overdue",
					"Partly Paid",
					"Overdue and Discounted",
					"Unpaid and Discounted",
					"Partly Paid and Discounted",
				],
			],
		}
		if company_name:
			outstanding_filters['company'] = company_name
		outstanding_rows = frappe.get_all(
			'Sales Invoice',
			filters=outstanding_filters,
			fields=["customer", "company", "pos_profile", "grand_total", "paid_amount", "outstanding_amount"],
			page_length=0,
		)
		for row in outstanding_rows:
			if not _invoice_matches_profile(row, profile_name):
				continue
			customer_name = str(row.get("customer") or "").strip()
			if not customer_name:
				continue
			outstanding_amount = _as_float(
				row.get("outstanding_amount")
				or (row.get("grand_total") or 0) - (row.get("paid_amount") or 0)
			)
			if outstanding_amount <= 0:
				continue
			bucket = outstanding_by_customer.setdefault(
				customer_name,
				{"outstanding": 0.0, "pending_invoices_count": 0},
			)
			bucket["outstanding"] = float(bucket.get("outstanding") or 0.0) + outstanding_amount
			bucket["pending_invoices_count"] = int(bucket.get("pending_invoices_count") or 0) + 1

	def _resolve_credit_limit(credit_limits: list[dict[str, Any]]) -> float | None:
		if not credit_limits:
			return None
		if company_name:
			for credit_row in credit_limits:
				company = str(credit_row.get("company") or "").strip()
				if company == company_name:
					try:
						return float(credit_row.get("credit_limit"))
					except Exception:
						return None
		for credit_row in credit_limits:
			try:
				return float(credit_row.get("credit_limit"))
			except Exception:
				continue
		return None

	for customer in customers:
		credit_limits = credit_by_parent.get(customer.get("name"), [])
		customer["credit_limits"] = credit_limits
		summary = outstanding_by_customer.get(customer.get("name"), {"outstanding": 0.0, "pending_invoices_count": 0})
		outstanding = float(summary.get("outstanding") or 0.0)
		pending_count = int(summary.get("pending_invoices_count") or 0)
		credit_limit = _resolve_credit_limit(credit_limits)
		available_credit = (credit_limit - outstanding) if credit_limit is not None else None
		customer["outstanding"] = outstanding
		customer["total_outstanding"] = outstanding
		customer["currentBalance"] = outstanding
		customer["pending_invoices_count"] = pending_count
		customer["pendingInvoices"] = pending_count
		customer["totalPendingAmount"] = outstanding
		customer["pendingInvoicesCount"] = pending_count
		customer["available_credit"] = available_credit
		customer["availableCredit"] = available_credit
		customer["receivable_accounts"] = receivable_accounts_by_customer.get(customer.get("name"), [])
		customer["supplier_accounts"] = supplier_accounts_by_customer.get(customer.get("name"), [])
		if customer.get("default_currency"):
			customer["party_account_currency"] = customer.get("default_currency")
		elif company_name:
			customer["party_account_currency"] = frappe.db.get_value("Company", company_name, "default_currency")
		else:
			customer["party_account_currency"] = None
	return customers


def _get_suppliers(
	*,
	company_name: str | None = None,
	modified_since: str | None = None,
	include_disabled: bool = False,
	offset: int = 0,
	limit: int = 0,
) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "Supplier"):
		return []
	supplier_fields = _get_doctype_fieldnames("Supplier")
	filters: dict[str, Any] = {}
	if not include_disabled and "disabled" in supplier_fields:
		filters["disabled"] = 0
	if modified_since:
		filters["modified"] = [">=", modified_since]

	selected_fields = [
		"name",
		"supplier_name",
		"supplier_group",
		"supplier_type",
		"default_currency",
		"default_bank_account",
		"payment_terms",
		"is_internal_supplier",
		"represents_company",
		"disabled",
	]
	limit_value = max(int(limit or 0), 0)
	start_value = max(int(offset or 0), 0)
	suppliers = frappe.get_all(
		"Supplier",
		filters=filters,
		fields=selected_fields,
		order_by="supplier_name asc",
		page_length=0,
		limit_page_length=limit_value if limit_value > 0 else None,
		limit_start=start_value if limit_value > 0 else None,
	)
	bank_accounts: list[str] = []
	for row in suppliers:
		account = str(row.get("default_bank_account") or "").strip()
		if account:
			bank_accounts.append(account)

	bank_account_by_name: dict[str, dict[str, Any]] = {}
	if bank_accounts and frappe.db.exists("DocType", "Bank Account"):
		bank_account_rows = frappe.get_all(
			"Bank Account",
			filters={"name": ["in", bank_accounts]},
			fields=["name", "account", "account_name", "bank", "company", "is_company_account"],
			page_length=0,
		)
		bank_account_by_name = {row.get("name"): row for row in bank_account_rows if row.get("name")}

	for supplier in suppliers:
		supplier["supplier_name"] = str(supplier.get("supplier_name") or supplier.get("name") or "").strip()
		supplier["supplier_group"] = str(supplier.get("supplier_group") or "").strip() or None
		supplier["supplier_type"] = str(supplier.get("supplier_type") or "").strip() or None
		supplier["default_currency"] = str(supplier.get("default_currency") or "").strip() or None
		supplier["default_bank_account"] = str(supplier.get("default_bank_account") or "").strip() or None
		supplier["payment_terms"] = str(supplier.get("payment_terms") or "").strip() or None
		supplier["is_internal_supplier"] = 1 if to_bool(supplier.get("is_internal_supplier"), default=False) else 0
		supplier["represents_company"] = str(supplier.get("represents_company") or "").strip() or None
		supplier["disabled"] = 1 if to_bool(supplier.get("disabled"), default=False) else 0
		bank_account = bank_account_by_name.get(supplier.get("default_bank_account")) if supplier.get("default_bank_account") else None
		if bank_account:
			supplier["bank_account"] = {
				"name": bank_account.get("name"),
				"account": bank_account.get("account"),
				"account_name": bank_account.get("account_name"),
				"bank": bank_account.get("bank"),
				"company": bank_account.get("company"),
				"is_company_account": 1 if to_bool(bank_account.get("is_company_account"), default=False) else 0,
			}
		else:
			supplier["bank_account"] = None
		if not supplier.get("default_currency") and company_name:
			supplier["default_currency"] = frappe.db.get_value("Company", company_name, "default_currency")
	return suppliers

def _get_sales_invoices_delta(*, modified_since: str, profile_name: str | None) -> list[dict[str, Any]]:
	filters: dict[str, Any] = {"modified": [">=", modified_since]}
	if profile_name:
		profile_detail = _get_pos_profile_detail(profile_name) or {}
		profile_company = str(profile_detail.get("company") or "").strip()
		if profile_company:
			filters["company"] = profile_company

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=_sales_invoice_base_fields(),
		order_by="modified asc",
		page_length=0,
	)
	if profile_name:
		invoices = [row for row in invoices if _invoice_matches_profile(row, profile_name)]
	for row in invoices:
		row.setdefault("items", [])
		row.setdefault("payments", [])
		row.setdefault("payment_schedule", [])
	_attach_invoice_children(invoices)
	return _normalize_sales_invoice_rows(invoices)


def _get_payment_entries_delta(*, modified_since: str) -> list[dict[str, Any]]:
	entries = frappe.get_all(
		"Payment Entry",
		filters={"modified": [">=", modified_since], "party_type": "Customer", "payment_type": "Receive"},
		fields=_payment_entry_base_fields(),
		order_by="modified asc",
		page_length=0,
	)
	for row in entries:
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
	for row in frappe.get_all(
		"Bin",
		filters={"warehouse": warehouse, "modified": [">=", modified_since]},
		fields=["item_code"],
		page_length=0,
	):
		item_code = (row.get("item_code") or "").strip()
		if item_code:
			item_codes.add(item_code)

	for row in frappe.get_all(
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
	for row in frappe.get_all(
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
	alerts = _build_inventory_alerts(warehouse=warehouse, items=items)
	items = _apply_inventory_visibility_rules(items=items, alerts=alerts)
	return _attach_alerts_to_inventory_items(items=items, alerts=alerts)


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
	rows = frappe.get_all(
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
	rows = frappe.get_all(
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

	bins = frappe.get_all(
		"Bin",
		filters={"warehouse": warehouse, "item_code": ["in", item_codes]},
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "projected_qty", "stock_uom", "valuation_rate"],
		page_length=0,
	)
	bin_by_code = {row.get("item_code"): row for row in bins if row.get("item_code")}

	items = frappe.get_all(
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
	prices = frappe.get_all(
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
			}
		)
	return output


def _get_invoices(
	profile_name: str | None,
	*,
	recent_paid_only: bool,
	company_name: str | None = None,
	offset: int = 0,
	limit: int = 0,
) -> list[dict[str, Any]]:
	if not profile_name and not company_name:
		return []
	if int(limit or 0) > 0 or int(offset or 0) > 0:
		return _get_invoices_paged(
			profile_name,
			recent_paid_only=recent_paid_only,
			company_name=company_name,
			offset=offset,
			limit=limit,
		)

	effective_company = str(company_name or "").strip() or None

	start_date = add_days(nowdate(), -90)  # olds invoices to fetch
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

	open_filters: dict[str, Any] = {"posting_date": [">=", start_date], "status": ["in", open_statuses]}
	if effective_company:
		open_filters["company"] = effective_company
	invoices = frappe.get_all(
		"Sales Invoice",
		filters=open_filters,
		fields=_sales_invoice_base_fields(),
		order_by="posting_date desc",
		page_length=0,
	)
	if recent_paid_only:
		paid_start = add_days(nowdate(), -7)  # Paid Days
		paid_statuses = ["Paid", "Paid and Discounted"]
		paid_filters: dict[str, Any] = {"posting_date": [">=", paid_start], "status": ["in", paid_statuses]}
		if effective_company:
			paid_filters["company"] = effective_company
		paid = frappe.get_all(
			"Sales Invoice",
			filters=paid_filters,
			fields=_sales_invoice_base_fields(),
			order_by="posting_date desc",
			page_length=0,
		)
		seen = {row.get("name") for row in invoices if row.get("name")}
		for row in paid:
			if row.get("name") not in seen:
				invoices.append(row)
				seen.add(row.get("name"))
	if profile_name:
		invoices = [row for row in invoices if _invoice_matches_profile(row, profile_name)]
	invoices.sort(
		key=lambda row: (
			str(row.get("posting_date") or ""),
			str(row.get("modified") or ""),
		),
		reverse=True,
	)
	for row in invoices:
		row.setdefault("items", [])
		row.setdefault("payments", [])
		row.setdefault("payment_schedule", [])
	_attach_invoice_children(invoices)
	return _normalize_sales_invoice_rows(invoices)


def _get_invoices_paged(
	profile_name: str | None,
	*,
	recent_paid_only: bool,
	company_name: str | None = None,
	offset: int = 0,
	limit: int = 0,
) -> list[dict[str, Any]]:
	if not profile_name and not company_name:
		return []
	effective_company = str(company_name or "").strip() or None

	start_date = add_days(nowdate(), -90) # invoice_days
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
	base_fields = ["name", "posting_date", "modified", "pos_profile"]
	open_filters: dict[str, Any] = {"posting_date": [">=", start_date], "status": ["in", open_statuses]}
	if effective_company:
		open_filters["company"] = effective_company
	open_rows = frappe.get_all(
		"Sales Invoice",
		filters=open_filters,
		fields=base_fields,
		order_by="posting_date desc",
		page_length=0,
	)
	candidates = {row.get("name"): row for row in open_rows if row.get("name")}
	if recent_paid_only:
		paid_start = add_days(nowdate(), -7)
		paid_statuses = ["Paid", "Paid and Discounted"]
		paid_filters: dict[str, Any] = {"posting_date": [">=", paid_start], "status": ["in", paid_statuses]}
		if effective_company:
			paid_filters["company"] = effective_company
		paid_rows = frappe.get_all(
			"Sales Invoice",
			filters=paid_filters,
			fields=base_fields,
			order_by="posting_date desc",
			page_length=0,
		)
		for row in paid_rows:
			name = row.get("name")
			if name:
				candidates.setdefault(name, row)

	ordered = [
		row
		for row in sorted(
			candidates.values(),
			key=lambda current: (str(current.get("posting_date") or ""), str(current.get("modified") or "")),
			reverse=True,
		)
		if _invoice_matches_profile(row, profile_name)
	]
	start = max(int(offset or 0), 0)
	limit_value = max(int(limit or 0), 0)
	if limit_value <= 0:
		return []
	selected = ordered[start : start + limit_value]
	if not selected:
		return []

	selected_names = [row.get("name") for row in selected if row.get("name")]
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", selected_names]},
		fields=_sales_invoice_base_fields(),
		page_length=0,
	)
	for row in rows:
		row.setdefault("items", [])
		row.setdefault("payments", [])
		row.setdefault("payment_schedule", [])
	_attach_invoice_children(rows)
	rows = _normalize_sales_invoice_rows(rows)
	order_index = {name: idx for idx, name in enumerate(selected_names)}
	rows.sort(key=lambda row: order_index.get(row.get("name"), 0))
	return rows


def _count_invoices_for_bootstrap(
	profile_name: str | None,
	*,
	recent_paid_only: bool,
	company_name: str | None = None,
) -> int:
	if not profile_name and not company_name:
		return 0
	effective_company = str(company_name or "").strip() or None

	start_date = add_days(nowdate(), -90)
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
	base_fields = ["name", "posting_date", "modified", "pos_profile"]
	open_filters: dict[str, Any] = {"posting_date": [">=", start_date], "status": ["in", open_statuses]}
	if effective_company:
		open_filters["company"] = effective_company
	open_rows = frappe.get_all(
		"Sales Invoice",
		filters=open_filters,
		fields=base_fields,
		page_length=0,
	)
	candidates = {row.get("name"): row for row in open_rows if row.get("name")}
	if recent_paid_only:
		paid_start = add_days(nowdate(), -7)
		paid_statuses = ["Paid", "Paid and Discounted"]
		paid_filters: dict[str, Any] = {"posting_date": [">=", paid_start], "status": ["in", paid_statuses]}
		if effective_company:
			paid_filters["company"] = effective_company
		paid_rows = frappe.get_all(
			"Sales Invoice",
			filters=paid_filters,
			fields=base_fields,
			page_length=0,
		)
		for row in paid_rows:
			name = row.get("name")
			if name:
				candidates.setdefault(name, row)

	ordered = [
		row
		for row in sorted(
			candidates.values(),
			key=lambda current: (str(current.get("posting_date") or ""), str(current.get("modified") or "")),
			reverse=True,
		)
		if _invoice_matches_profile(row, profile_name)
	]
	return len(ordered)


def _get_payment_entries(from_date: str, *, offset: int = 0, limit: int = 0) -> list[dict[str, Any]]:
	base_fields = _payment_entry_base_fields()
	limit_value = max(int(limit or 0), 0)
	start_value = max(int(offset or 0), 0)
	rows = frappe.get_all(
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
		limit_page_length=limit_value if limit_value > 0 else None,
		limit_start=start_value if limit_value > 0 else None,
	)
	for row in rows:
		for fieldname in base_fields:
			row.setdefault(fieldname, None)
		row.setdefault("references", [])
	_attach_payment_entry_references(rows)
	return _normalize_payment_entry_rows(rows)


def _get_payment_out_entries(from_date: str, *, offset: int = 0, limit: int = 0) -> list[dict[str, Any]]:
	base_fields = _payment_entry_base_fields()
	limit_value = max(int(limit or 0), 0)
	start_value = max(int(offset or 0), 0)
	rows = frappe.get_all(
		"Payment Entry",
		filters={
			"posting_date": [">=", from_date],
			"docstatus": 1,
			"party_type": "Supplier",
			"payment_type": "Pay",
		},
		fields=base_fields,
		order_by="posting_date desc",
		page_length=0,
		limit_page_length=limit_value if limit_value > 0 else None,
		limit_start=start_value if limit_value > 0 else None,
	)
	for row in rows:
		for fieldname in base_fields:
			row.setdefault(fieldname, None)
		row.setdefault("references", [])
	_attach_payment_entry_references(rows)
	return _normalize_payment_entry_rows(rows)


def _get_internal_transfer_entries(from_date: str, *, offset: int = 0, limit: int = 0) -> list[dict[str, Any]]:
	base_fields = _payment_entry_base_fields()
	limit_value = max(int(limit or 0), 0)
	start_value = max(int(offset or 0), 0)
	rows = frappe.get_all(
		'Payment Entry',
		filters={
			'posting_date': [">=", from_date],
			'docstatus': 1,
			'payment_type': 'Internal Transfer',
		},
		fields=base_fields,
		order_by="posting_date desc",
		page_length=0,
		limit_page_length=limit_value if limit_value > 0 else None,
		limit_start=start_value if limit_value > 0 else None,
	)
	for row in rows:
		for fieldname in base_fields:
			row.setdefault(fieldname, None)
		row.setdefault("references", [])
	_attach_payment_entry_references(rows)
	return _normalize_payment_entry_rows(rows)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def bootstrap(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)

	include_inventory = to_bool(value_from_aliases(body, "include_inventory", "includeInventory"), default=True)
	include_customers = to_bool(value_from_aliases(body, "include_customers", "includeCustomers"), default=True)
	include_suppliers = to_bool(value_from_aliases(body, "include_suppliers", "includeSuppliers"), default=True)
	include_invoices = to_bool(value_from_aliases(body, "include_invoices", "includeInvoices"), default=True)
	include_alerts = to_bool(value_from_aliases(body, "include_alerts", "includeAlerts"), default=True)
	include_payment_out = to_bool(value_from_aliases(body, "include_payment_out", "includePaymentOut"), default=True)
	include_internal_transfers = to_bool(
		value_from_aliases(body, "include_internal_transfers", "includeInternalTransfers"),
		default=True,
	)
	recent_paid_only = to_bool(value_from_aliases(body, "recent_paid_only", "recentPaidOnly"), default=True)
	dummy_alerts = to_bool(value_from_aliases(body, "dummy_alerts", "dummyAlerts"), default=False)
	default_page_size = 50
	inventory_offset = _as_int(
		value_from_aliases(
			body,
			"inventory_offset",
			"inventoryOffset",
			default=value_from_aliases(body, "offset", default=0),
		),
		0,
	)
	inventory_limit = _as_int(
		value_from_aliases(
			body,
			"inventory_limit",
			"inventoryLimit",
			default=value_from_aliases(body, "limit", "page_size", "pageSize", default=default_page_size),
		),
		default_page_size,
	)
	customer_offset = _as_int(value_from_aliases(body, "customer_offset", "customerOffset", default=0), 0)
	supplier_offset = _as_int(value_from_aliases(body, "supplier_offset", "supplierOffset", default=0), 0)
	customer_limit = _as_int(
		value_from_aliases(body, "customer_limit", "customerLimit", default=default_page_size),
		default_page_size,
	)
	supplier_limit = _as_int(
		value_from_aliases(body, "supplier_limit", "supplierLimit", default=default_page_size),
		default_page_size,
	)
	invoice_offset = _as_int(value_from_aliases(body, "invoice_offset", "invoiceOffset", default=0), 0)
	invoice_limit = _as_int(value_from_aliases(body, "invoice_limit", "invoiceLimit", default=default_page_size), default_page_size)
	payment_entry_offset = _as_int(
		value_from_aliases(body, "payment_entry_offset", "paymentEntryOffset", default=0),
		0,
	)
	payment_entry_limit = _as_int(
		value_from_aliases(body, "payment_entry_limit", "paymentEntryLimit", default=default_page_size),
		default_page_size,
	)
	payment_out_offset = _as_int(
		value_from_aliases(body, "payment_out_offset", "paymentOutOffset", default=0),
		0,
	)
	payment_out_limit = _as_int(
		value_from_aliases(body, "payment_out_limit", "paymentOutLimit", default=default_page_size),
		default_page_size,
	)
	internal_transfer_offset = _as_int(
		value_from_aliases(body, "internal_transfer_offset", "internalTransferOffset", default=0),
		0,
	)
	internal_transfer_limit = _as_int(
		value_from_aliases(body, "internal_transfer_limit", "internalTransferLimit", default=default_page_size),
		default_page_size,
	)
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

	profile_summaries = _get_accessible_pos_profiles(frappe.session.user)
	accessible_profile_names = {row.get("name") for row in profile_summaries if row.get("name")}
	if requested_profile_name and requested_profile_name not in accessible_profile_names:
		frappe.throw(f"User {frappe.session.user} does not have access to POS Profile {requested_profile_name}.")
	if not profile_name and profile_summaries:
		profile_name = profile_summaries[0].get("name")

	open_shift = _get_open_shift(requested_profile_name or None, pos_opening_entry_name or None) or {}
	open_shift_required = False
	pos_closing_entry = None
	if not open_shift:
		open_shift_required = True
		pos_closing_entry = _get_latest_pos_closing_entry(
			user=frappe.session.user,
			profile_name=profile_name or None,
			opening_name=pos_opening_entry_name or None,
		)
	if not requested_profile_name and open_shift.get("pos_profile"):
		profile_name = open_shift.get("pos_profile")
	if profile_name and profile_name not in accessible_profile_names:
		frappe.throw(f"User {frappe.session.user} does not have access to POS Profile {profile_name}.")

	pos_profile_detail = _get_pos_profile_detail(profile_name) if profile_name else None
	profiles: list[dict[str, Any]] = []
	for summary in profile_summaries:
		current_name = str(summary.get("name") or "").strip()
		if not current_name:
			continue
		if pos_profile_detail and current_name == str((pos_profile_detail or {}).get("name") or "").strip():
			detail = dict(pos_profile_detail)
		else:
			detail = _get_pos_profile_detail(current_name) or {}
		if not detail:
			detail = {
				"name": current_name,
				"profileName": current_name,
				"company": str(summary.get("company") or "").strip(),
				"currency": str(summary.get("currency") or "").strip(),
				"payments": [],
			}
		detail["name"] = str(detail.get("name") or current_name)
		detail["profile_name"] = str(detail.get("profile_name") or detail["name"])
		detail.pop("profileName", None)
		detail["company"] = str(detail.get("company") or summary.get("company") or "").strip()
		detail["currency"] = str(detail.get("currency") or summary.get("currency") or "").strip()
		if not isinstance(detail.get("payments"), list):
			detail["payments"] = []
		profiles.append(detail)
	company_name = (
		(pos_profile_detail or {}).get("company")
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_value("Company", {}, "name")
	)
	company = {}
	if company_name:
		company = (
			frappe.get_all(
				"Company",
				filters={"name": company_name},
				fields=["name as company", "default_currency", "country", "tax_id", "default_receivable_account", "monthly_sales_target"],
				limit_page_length=1,
			)[0]
			if frappe.db.exists("Company", company_name)
			else {}
		)
		if company.get("default_receivable_account"):
			account_currency = frappe.db.get_value("Account", company.get("default_receivable_account"), "account_currency")
			company["default_receivable_account_currency"] = account_currency
		else:
			company["default_receivable_account_currency"] = None

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
	territory = (
		(str(value_from_aliases(body, "territory", default="") or ""))
	).strip()

	inventory_items: list[dict[str, Any]] = []
	inventory_total = 0
	inventory_alerts: list[dict[str, Any]] = []
	computed_inventory_alerts: list[dict[str, Any]] = []
	if include_inventory and warehouse and inventory_limit > 0:
		inventory_items = _build_inventory_items(
			warehouse=warehouse,
			price_list=price_list,
			offset=inventory_offset,
			limit=inventory_limit,
		)
	if include_inventory:
		inventory_total = int(
			frappe.db.count("Item", filters={"disabled": 0, "is_sales_item": 1})
			if frappe.db.exists("DocType", "Item")
			else 0
		)
	if warehouse and inventory_items and (include_alerts or _has_negative_inventory_rows(inventory_items)):
		computed_inventory_alerts = _build_inventory_alerts(warehouse=warehouse, items=inventory_items)
		inventory_items = _apply_inventory_visibility_rules(items=inventory_items, alerts=computed_inventory_alerts)
	if inventory_items:
		inventory_items = _attach_alerts_to_inventory_items(items=inventory_items, alerts=computed_inventory_alerts)
	if include_alerts:
		inventory_alerts = computed_inventory_alerts
	if include_alerts and dummy_alerts and not inventory_alerts:
		inventory_alerts = _build_dummy_inventory_alerts(inventory_items)

	customers: list[dict[str, Any]] = []
	customers_total = 0
	if include_customers and customer_limit > 0:
		customers = _get_customers(
			territory=territory or None,
			profile_name=profile_name or None,
			company_name=company_name or None,
			offset=customer_offset,
			limit=customer_limit,
		)
	if include_customers:
		customer_fields = _get_doctype_fieldnames("Customer")
		customer_filters: dict[str, Any] = {"disabled": 0}
		if territory and "territory" in customer_fields:
			customer_filters["territory"] = territory
		customers_total = int(
			frappe.db.count("Customer", filters=customer_filters)
			if frappe.db.exists("DocType", "Customer")
			else 0
		)

	suppliers: list[dict[str, Any]] = []
	suppliers_total = 0
	if include_suppliers and supplier_limit > 0:
		suppliers = _get_suppliers(
			company_name=company_name or None,
			modified_since=None,
			include_disabled=False,
			offset=supplier_offset,
			limit=supplier_limit,
		)
	if include_suppliers:
		suppliers_total = int(
			frappe.db.count("Supplier", filters={"disabled": 0})
			if frappe.db.exists("DocType", "Supplier")
			else 0
		)

	invoices: list[dict[str, Any]] = []
	invoices_total = 0
	if include_invoices and invoice_limit > 0:
		invoices = _get_invoices(
			profile_name,
			recent_paid_only=recent_paid_only,
			company_name=company_name or None,
			offset=invoice_offset,
			limit=invoice_limit,
		)
	if include_invoices:
		invoices_total = _count_invoices_for_bootstrap(
			profile_name,
			recent_paid_only=recent_paid_only,
			company_name=company_name or None,
		)

	payment_entries: list[dict[str, Any]] = []
	payment_entries_total = 0
	from_date = str(value_from_aliases(body, "from_date", "fromDate", default=add_days(nowdate(), -30)))
	if payment_entry_limit > 0:
		payment_entries = _get_payment_entries(
			from_date=from_date,
			offset=payment_entry_offset,
			limit=payment_entry_limit,
		)
	if frappe.db.exists("DocType", "Payment Entry"):
		payment_entries_total = int(
			frappe.db.count(
				"Payment Entry",
				filters={
					"posting_date": [">=", from_date],
					"docstatus": 1,
					"party_type": "Customer",
					"payment_type": "Receive",
				},
			)
		)
	payment_out_entries: list[dict[str, Any]] = []
	payment_out_total = 0
	if include_payment_out and payment_out_limit > 0:
		payment_out_entries = _get_payment_out_entries(
			from_date=from_date,
			offset=payment_out_offset,
			limit=payment_out_limit,
		)
	if include_payment_out and frappe.db.exists("DocType", "Payment Entry"):
		payment_out_total = int(
			frappe.db.count(
				"Payment Entry",
				filters={
					"posting_date": [">=", from_date],
					"docstatus": 1,
					"party_type": "Supplier",
					"payment_type": "Pay",
				},
			)
		)
	internal_transfers: list[dict[str, Any]] = []
	internal_transfers_total = 0
	if include_internal_transfers and internal_transfer_limit > 0:
		internal_transfers = _get_internal_transfer_entries(
			from_date=from_date,
			offset=internal_transfer_offset,
			limit=internal_transfer_limit,
		)
	if include_internal_transfers and frappe.db.exists("DocType", "Payment Entry"):
		internal_transfers_total = int(
			frappe.db.count(
				"Payment Entry",
				filters={
					"posting_date": [">=", from_date],
					"docstatus": 1,
					"payment_type": "Internal Transfer",
				},
			)
		)

	only_other_cashiers = to_bool(
		value_from_aliases(body, "only_other_cashiers", "onlyOtherCashiers", default=True),
		default=True,
	)
	currency_base = ((company or {}).get("default_currency") or (pos_profile_detail or {}).get("currency") or "").strip()
	exchange_rate_date = str(
		open_shift.get("posting_date")
		or (pos_closing_entry or {}).get("posting_date")
		or nowdate()
	)
	currencies, exchange_rates = _get_active_currencies(
		base_currency=currency_base or None,
		rate_date=exchange_rate_date,
	)
	stock_settings = _get_single_row(
		"Stock Settings",
		fields=["allow_negative_stock"],
		defaults={"allow_negative_stock": 0},
	)
	payment_terms = frappe.get_all(
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

	customer_groups = frappe.get_all(
		"Customer Group",
		fields=["name", "customer_group_name", "is_group", "parent_customer_group"],
		page_length=0,
	)
	territories = frappe.get_all(
		"Territory",
		fields=["name", "territory_name", "is_group", "parent_territory"],
		page_length=0,
	)
	product_categories = frappe.get_all(
		"Item Group",
		fields=["name", "item_group_name", "is_group", "parent_item_group"],
		page_length=0,
	)
	selected_profile_payments = []
	for row in profiles:
		if str(row.get("name") or "").strip() == str(profile_name or "").strip():
			selected_profile_payments = list(row.get("payments") or [])
			break

	companies: list[dict[str, Any]] = []
	company_names = sorted(
		{
			str(row.get("company") or "").strip()
			for row in profiles
			if str(row.get("company") or "").strip()
		}
		| ({str(company_name).strip()} if company_name else set())
	)
	if company_names:
		company_rows = frappe.get_all(
			"Company",
			filters={"name": ["in", company_names]},
			fields=[
				"name as company",
				"default_currency",
				"country",
				"tax_id",
				"default_receivable_account",
			],
			page_length=0,
		)
		account_names = [row.get("default_receivable_account") for row in company_rows if row.get("default_receivable_account")]
		account_currency_by_name = {}
		if account_names and frappe.db.exists("DocType", "Account"):
			account_currency_by_name = {
				row.get("name"): row.get("account_currency")
				for row in frappe.get_all(
					"Account",
					filters={"name": ["in", account_names]},
					fields=["name", "account_currency"],
					page_length=0,
				)
				if row.get("name")
			}
		for row in company_rows:
			acc = row.get("default_receivable_account")
			row["default_receivable_account_currency"] = account_currency_by_name.get(acc)
			companies.append(row)

	company_accounts: list[dict[str, Any]] = []
	if company_names and frappe.db.exists("DocType", "Account"):
		account_fieldnames = _get_doctype_fieldnames("Account")
		account_fields = [
			"name",
			"account_name",
			"account_type",
			"account_currency",
			"company",
			"is_group",
			"disabled",
		]
		account_fields = [field for field in account_fields if field in account_fieldnames or field == "name"]
		account_filters: dict[str, Any] = {"company": ["in", company_names], "is_group": 0}
		if "account_type" in account_fieldnames:
			account_filters["account_type"] = ["in", ["Bank", "Cash"]]
		company_accounts = frappe.get_all(
			"Account",
			filters=account_filters,
			fields=account_fields,
			page_length=0,
		)
		for row in company_accounts:
			row["account_name"] = row.get("account_name") or row.get("name")
			row["account_type"] = row.get("account_type") or None
			row["account_currency"] = row.get("account_currency") or None
			row["company"] = row.get("company") or None
			row["disabled"] = 1 if to_bool(row.get("disabled"), default=False) else 0

	data: dict[str, Any] = {
		"context": {
			"profile_name": profile_name,
			"company": company_name,
			"company_currency": (company or {}).get("default_currency"),
			"warehouse": warehouse or None,
			"territory": territory or None,
			"price_list": price_list or None,
			"currency": (pos_profile_detail or {}).get("currency"),
			"party_account_currency": (company or {}).get("default_currency"),
			"monthly_sales_target": (company or {}).get("monthly_sales_target"),
		},
		"open_shift": open_shift or None,
		"open_shift_required": 1 if open_shift_required else 0,
		"pos_closing_entry": pos_closing_entry,
		"pos_profiles": profiles,
		"payment_methods": selected_profile_payments,
		"companies": companies,
		"company_accounts": company_accounts,
		"stock_settings": stock_settings or {"allow_negative_stock": 0},
		"currencies": currencies,
		"exchange_rates": exchange_rates,
		"payment_terms": payment_terms,
		"shipping_rules": get_shipping_rules(),
		"customer_groups": customer_groups,
		"territories": territories,
		"categories": product_categories,
	}

	if include_inventory:
		data["inventory"] = {
			"items": inventory_items,
			"pagination": _build_pagination(inventory_offset, inventory_limit, inventory_total, len(inventory_items)),
		}
	if include_alerts:
		data["inventory_alerts"] = inventory_alerts
	if include_customers:
		data["customers"] = {
			"items": customers,
			"pagination": _build_pagination(customer_offset, customer_limit, customers_total, len(customers)),
		}

	if include_suppliers:
		data["suppliers"] = {
			"items": suppliers,
			"pagination": _build_pagination(supplier_offset, supplier_limit, suppliers_total, len(suppliers)),
		}
	if include_invoices:
		data["invoices"] = {
			"items": invoices,
			"pagination": _build_pagination(invoice_offset, invoice_limit, invoices_total, len(invoices)),
		}
	if payment_entry_limit > 0:
		data["payment_entries"] = {
			"items": payment_entries,
			"pagination": _build_pagination(payment_entry_offset, payment_entry_limit, payment_entries_total, len(payment_entries)),
		}
	if include_payment_out:
		data["payment_out"] = {
			"items": payment_out_entries,
			"pagination": _build_pagination(payment_out_offset, payment_out_limit, payment_out_total, len(payment_out_entries)),
		}
	if include_internal_transfers:
		data["internal_transfers"] = {
			"items": internal_transfers,
			"pagination": _build_pagination(
				internal_transfer_offset,
				internal_transfer_limit,
				internal_transfers_total,
				len(internal_transfers),
			),
		}

	return ok(data)
	# 2807
