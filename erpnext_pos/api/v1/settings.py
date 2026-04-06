from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import frappe

from .common import ok, parse_payload, standard_api_response


SETTINGS_DOCTYPE = "ERPNext POS Settings"


@dataclass(frozen=True)
class POSAPISettings:
	enable_api: bool = True
	allow_discovery: bool = True
	allow_client_secret_response: bool = False
	allowed_api_roles: tuple[str, ...] = ("System Manager", "POS", "POS User")
	allowed_api_users: tuple[str, ...] = ()
	api_version: str = "v1"
	default_sync_page_size: int = 50
	bootstrap_invoice_days: int = 90
	recent_paid_invoice_days: int = 7
	enable_inventory_alerts: bool = True
	inventory_alert_default_limit: int = 20
	inventory_alert_critical_ratio: float = 0.35
	inventory_alert_low_ratio: float = 1.0


def _to_bool(value: Any, default: bool = False) -> bool:
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


def _to_int(value: Any, default: int) -> int:
	try:
		return int(value)
	except Exception:
		return default


def _to_float(value: Any, default: float) -> float:
	try:
		return float(value)
	except Exception:
		return default


def _has_any_key(source: dict[str, Any] | None, *keys: str) -> bool:
	if not isinstance(source, dict):
		return False
	return any(key in source for key in keys)


def _as_list(value: Any) -> list[Any]:
	if isinstance(value, list):
		return value
	if isinstance(value, tuple):
		return list(value)
	if isinstance(value, set):
		return list(value)
	return []


def _normalize_name_rows(value: Any, fieldname: str) -> list[str]:
	if isinstance(value, str):
		candidates = [part.strip() for part in value.replace("\n", ",").split(",")]
	elif isinstance(value, (list, tuple, set)):
		candidates = []
		for row in value:
			if isinstance(row, dict):
				candidates.append(str(row.get(fieldname) or "").strip())
			else:
				candidates.append(str(row or "").strip())
	else:
		candidates = []

	output: list[str] = []
	seen: set[str] = set()
	for candidate in candidates:
		if not candidate or candidate in seen:
			continue
		seen.add(candidate)
		output.append(candidate)
	return output


def _clear_settings_cache() -> None:
	if hasattr(frappe.local, "erpnext_pos_settings_cache"):
		delattr(frappe.local, "erpnext_pos_settings_cache")


def _get_settings_doc():
	return frappe.get_single(SETTINGS_DOCTYPE)


def _settings_meta():
	return frappe.get_meta(SETTINGS_DOCTYPE)


def _has_field(fieldname: str) -> bool:
	return bool(_settings_meta().get_field(fieldname))


def _child_table_doctype(fieldname: str) -> str | None:
	field = _settings_meta().get_field(fieldname)
	return str(field.options or "").strip() or None if field else None


def _read_name_list(doc, table_fieldname: str, row_fieldname: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
	if not _has_field(table_fieldname):
		return fallback
	return tuple(_normalize_name_rows(doc.get(table_fieldname), row_fieldname)) or fallback


def _read_table_rows(doc, table_fieldname: str, allowed_fields: tuple[str, ...]) -> list[dict[str, Any]]:
	if not _has_field(table_fieldname):
		return []
	rows = []
	for row in _as_list(doc.get(table_fieldname)):
		if not hasattr(row, "as_dict"):
			continue
		raw = row.as_dict()
		rows.append({fieldname: raw.get(fieldname) for fieldname in allowed_fields if fieldname in raw})
	return rows


def get_settings() -> POSAPISettings:
	cached = getattr(frappe.local, "erpnext_pos_settings_cache", None)
	if cached:
		return cached

	defaults = POSAPISettings()
	doc = _get_settings_doc()

	settings = POSAPISettings(
		enable_api=_to_bool(doc.get("enable_api"), defaults.enable_api),
		allow_discovery=_to_bool(doc.get("allow_discovery"), defaults.allow_discovery),
		allow_client_secret_response=_to_bool(
			doc.get("allow_client_secret_response"), defaults.allow_client_secret_response
		),
		allowed_api_roles=_read_name_list(doc, "allowed_api_roles_table", "role", defaults.allowed_api_roles),
		allowed_api_users=_read_name_list(doc, "allowed_api_users", "user", defaults.allowed_api_users),
		api_version="v1",
		default_sync_page_size=_to_int(doc.get("default_sync_page_size"), defaults.default_sync_page_size),
		bootstrap_invoice_days=_to_int(doc.get("bootstrap_invoice_days"), defaults.bootstrap_invoice_days),
		recent_paid_invoice_days=_to_int(doc.get("recent_paid_invoice_days"), defaults.recent_paid_invoice_days),
		enable_inventory_alerts=_to_bool(doc.get("enable_inventory_alerts"), defaults.enable_inventory_alerts),
		inventory_alert_default_limit=_to_int(
			doc.get("inventory_alert_default_limit"), defaults.inventory_alert_default_limit
		),
		inventory_alert_critical_ratio=_to_float(
			doc.get("inventory_alert_critical_ratio"), defaults.inventory_alert_critical_ratio
		),
		inventory_alert_low_ratio=_to_float(doc.get("inventory_alert_low_ratio"), defaults.inventory_alert_low_ratio),
	)
	frappe.local.erpnext_pos_settings_cache = settings
	return settings


def _build_settings_payload(*, include_options: bool = False) -> dict[str, Any]:
	settings = get_settings()
	doc = _get_settings_doc()

	data: dict[str, Any] = {
		"enable_api": settings.enable_api,
		"allow_discovery": settings.allow_discovery,
		"allow_client_secret_response": settings.allow_client_secret_response,
		"allowed_api_roles": list(settings.allowed_api_roles),
		"allowed_api_users": list(settings.allowed_api_users),
		"api_version": settings.api_version,
		"default_sync_page_size": settings.default_sync_page_size,
		"bootstrap_invoice_days": settings.bootstrap_invoice_days,
		"recent_paid_invoice_days": settings.recent_paid_invoice_days,
		"enable_inventory_alerts": settings.enable_inventory_alerts,
		"inventory_alert_default_limit": settings.inventory_alert_default_limit,
		"inventory_alert_critical_ratio": settings.inventory_alert_critical_ratio,
		"inventory_alert_low_ratio": settings.inventory_alert_low_ratio,
		"company": doc.get("company"),
		"mobile_oauth_client": doc.get("mobile_oauth_client"),
		"desktop_oauth_client": doc.get("desktop_oauth_client"),
		"user_role_bindings": _read_table_rows(doc, "user_role_bindings", ("enabled", "user", "role")),
		"inventory_alert_rules": _read_table_rows(
			doc,
			"inventory_alert_rules",
			("enabled", "warehouse", "item_group", "critical_ratio", "low_ratio", "priority"),
		),
	}

	if include_options:
		data["options"] = {
			"roles": frappe.get_all("Role", pluck="name", order_by="name asc", page_length=0)
			if frappe.db.exists("DocType", "Role")
			else [],
			"users": frappe.get_all(
				"User",
				filters={"enabled": 1},
				fields=["name", "full_name", "user_type"],
				order_by="name asc",
				page_length=0,
			)
			if frappe.db.exists("DocType", "User")
			else [],
			"warehouses": frappe.get_all(
				"Warehouse",
				fields=["name", "warehouse_name", "is_group"],
				order_by="name asc",
				page_length=0,
			)
			if frappe.db.exists("DocType", "Warehouse")
			else [],
			"item_groups": frappe.get_all(
				"Item Group",
				fields=["name", "item_group_name", "is_group"],
				order_by="name asc",
				page_length=0,
			)
			if frappe.db.exists("DocType", "Item Group")
			else [],
		}

	return data


def _replace_simple_name_table(doc, table_fieldname: str, row_fieldname: str, values: Any) -> None:
	if not _has_field(table_fieldname):
		return
	table_doctype = _child_table_doctype(table_fieldname)
	if not table_doctype:
		return

	doc.set(table_fieldname, [])
	for value in _normalize_name_rows(values, row_fieldname):
		doc.append(table_fieldname, {"doctype": table_doctype, row_fieldname: value})


def _replace_user_role_bindings(doc, values: Any) -> None:
	if not _has_field("user_role_bindings"):
		return
	table_doctype = _child_table_doctype("user_role_bindings")
	if not table_doctype:
		return

	doc.set("user_role_bindings", [])
	for raw in _as_list(values):
		if not isinstance(raw, dict):
			continue
		user = str(_first_present(raw, "user", default="") or "").strip()
		role = str(_first_present(raw, "role", default="") or "").strip()
		if not user or not role:
			continue
		doc.append(
			"user_role_bindings",
			{
				"doctype": table_doctype,
				"enabled": 1 if _to_bool(_first_present(raw, "enabled"), True) else 0,
				"user": user,
				"role": role,
			},
		)


def _replace_inventory_alert_rules(doc, values: Any) -> None:
	if not _has_field("inventory_alert_rules"):
		return
	table_doctype = _child_table_doctype("inventory_alert_rules")
	if not table_doctype:
		return

	doc.set("inventory_alert_rules", [])
	for raw in _as_list(values):
		if not isinstance(raw, dict):
			continue
		critical_ratio = max(0.0, _to_float(raw.get("critical_ratio"), 0.35))
		low_ratio = max(critical_ratio, _to_float(raw.get("low_ratio"), 1.0))
		priority = max(0, _to_int(raw.get("priority"), 10))
		doc.append(
			"inventory_alert_rules",
			{
				"doctype": table_doctype,
				"enabled": 1 if _to_bool(raw.get("enabled"), True) else 0,
				"warehouse": raw.get("warehouse"),
				"item_group": raw.get("item_group"),
				"critical_ratio": critical_ratio,
				"low_ratio": low_ratio,
				"priority": priority,
			},
		)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def mobile_get(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)
	include_options = _to_bool(body.get("include_options"), False)
	_clear_settings_cache()
	return ok(_build_settings_payload(include_options=include_options))


@frappe.whitelist(methods=["POST"])
@standard_api_response
def mobile_update(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	frappe.only_for("System Manager")
	body = parse_payload(payload)
	settings_body = body.get("settings") if isinstance(body.get("settings"), dict) else body
	doc = _get_settings_doc()

	for fieldname, aliases, caster in (
		("enable_api", ("enable_api",), lambda value: 1 if _to_bool(value, False) else 0),
		("allow_discovery", ("allow_discovery",), lambda value: 1 if _to_bool(value, False) else 0),
		(
			"allow_client_secret_response",
			("allow_client_secret_response",),
			lambda value: 1 if _to_bool(value, False) else 0,
		),
		("default_sync_page_size", ("default_sync_page_size",), lambda value: _to_int(value, 50)),
		("bootstrap_invoice_days", ("bootstrap_invoice_days",), lambda value: _to_int(value, 90)),
		("recent_paid_invoice_days", ("recent_paid_invoice_days",), lambda value: _to_int(value, 7)),
		(
			"enable_inventory_alerts",
			("enable_inventory_alerts",),
			lambda value: 1 if _to_bool(value, False) else 0,
		),
		(
			"inventory_alert_default_limit",
			("inventory_alert_default_limit",),
			lambda value: _to_int(value, 20),
		),
		(
			"inventory_alert_critical_ratio",
			("inventory_alert_critical_ratio",),
			lambda value: _to_float(value, 0.35),
		),
		(
			"inventory_alert_low_ratio",
			("inventory_alert_low_ratio",),
			lambda value: _to_float(value, 1.0),
		),
		("company", ("company",), lambda value: value),
		("mobile_oauth_client", ("mobile_oauth_client",), lambda value: value),
		("desktop_oauth_client", ("desktop_oauth_client",), lambda value: value),
	):
		if _has_field(fieldname) and _has_any_key(settings_body, *aliases):
			doc.set(fieldname, caster(settings_body.get(aliases[0])))

	if _has_any_key(settings_body, "allowed_api_roles"):
		_replace_simple_name_table(
			doc,
			"allowed_api_roles_table",
			"role",
			settings_body.get("allowed_api_roles"),
		)
	if _has_any_key(settings_body, "allowed_api_users"):
		_replace_simple_name_table(
			doc,
			"allowed_api_users",
			"user",
			settings_body.get("allowed_api_users"),
		)
	if _has_any_key(settings_body, "user_role_bindings"):
		_replace_user_role_bindings(doc, settings_body.get("user_role_bindings"))
	if _has_any_key(settings_body, "inventory_alert_rules"):
		_replace_inventory_alert_rules(doc, settings_body.get("inventory_alert_rules"))

	doc.save(ignore_permissions=True)
	_clear_settings_cache()

	include_options = _to_bool(body.get("include_options"), False)
	return ok(_build_settings_payload(include_options=include_options))
