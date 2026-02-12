from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import frappe

from .common import to_bool


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


def _single_value(fieldname: str) -> Any:
	try:
		return frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname, cache=False)
	except Exception:
		return None


def _single_bool(fieldname: str, default: bool) -> bool:
	value = _single_value(fieldname)
	if value is None:
		return default
	if isinstance(value, str) and not value.strip():
		return default
	return to_bool(value, default=default)


def _single_roles(fieldname: str, default: tuple[str, ...]) -> tuple[str, ...]:
	value = _single_value(fieldname)
	if value is None:
		return default
	if isinstance(value, str):
		roles = tuple(r.strip() for r in value.split(",") if r and r.strip())
		return roles or default
	if isinstance(value, (list, tuple, set)):
		roles = tuple(str(r).strip() for r in value if str(r).strip())
		return roles or default
	return default


def _single_allowed_api_roles_table(default: tuple[str, ...] = ()) -> tuple[str, ...]:
	try:
		rows = frappe.get_all(
			"ERPNext POS API Role",
			filters={
				"parent": SETTINGS_DOCTYPE,
				"parenttype": SETTINGS_DOCTYPE,
				"parentfield": "allowed_api_roles_table",
			},
			pluck="role",
			page_length=0,
		)
	except Exception:
		return default

	roles: list[str] = []
	seen: set[str] = set()
	for row in rows:
		role = (row or "").strip()
		if not role or role in seen:
			continue
		seen.add(role)
		roles.append(role)
	return tuple(roles) or default


def _single_bound_roles(default: tuple[str, ...] = ()) -> tuple[str, ...]:
	try:
		rows = frappe.get_all(
			"ERPNext POS User Role",
			filters={
				"parent": SETTINGS_DOCTYPE,
				"parenttype": SETTINGS_DOCTYPE,
				"parentfield": "user_role_bindings",
				"enabled": 1,
			},
			pluck="role",
			page_length=0,
		)
	except Exception:
		return default

	roles: list[str] = []
	seen: set[str] = set()
	for row in rows:
		role = (row or "").strip()
		if not role or role in seen:
			continue
		seen.add(role)
		roles.append(role)
	return tuple(roles) or default


def _single_allowed_users(default: tuple[str, ...] = ()) -> tuple[str, ...]:
	try:
		rows = frappe.get_all(
			"ERPNext POS API User",
			filters={
				"parent": SETTINGS_DOCTYPE,
				"parenttype": SETTINGS_DOCTYPE,
				"parentfield": "allowed_api_users",
			},
			pluck="user",
			page_length=0,
		)
	except Exception:
		return default

	users: list[str] = []
	seen: set[str] = set()
	for row in rows:
		user = (row or "").strip()
		if not user or user in seen:
			continue
		seen.add(user)
		users.append(user)
	return tuple(users) or default


def _merge_names(*groups: tuple[str, ...]) -> tuple[str, ...]:
	result: list[str] = []
	seen: set[str] = set()
	for group in groups:
		for value in group or ():
			name = (value or "").strip()
			if not name or name in seen:
				continue
			seen.add(name)
			result.append(name)
	return tuple(result)


def get_settings() -> POSAPISettings:
	"""Load settings without relying on user DocType permissions."""
	cached = getattr(frappe.local, "erpnext_pos_settings_cache", None)
	if cached:
		return cached

	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		settings = POSAPISettings()
		frappe.local.erpnext_pos_settings_cache = settings
		return settings

	allowed_roles = _merge_names(
		_single_allowed_api_roles_table(),
		_single_roles("allowed_api_roles", ("System Manager", "POS", "POS User")),
		_single_bound_roles(),
	)
	settings = POSAPISettings(
		enable_api=_single_bool("enable_api", True),
		allow_discovery=_single_bool("allow_discovery", True),
		allow_client_secret_response=_single_bool("allow_client_secret_response", False),
		allowed_api_roles=allowed_roles or ("System Manager", "POS", "POS User"),
		allowed_api_users=_single_allowed_users(),
		api_version=str(_single_value("api_version") or "v1"),
		default_sync_page_size=_to_int(_single_value("default_sync_page_size"), 50),
		bootstrap_invoice_days=_to_int(_single_value("bootstrap_invoice_days"), 90),
		recent_paid_invoice_days=_to_int(_single_value("recent_paid_invoice_days"), 7),
		enable_inventory_alerts=_single_bool("enable_inventory_alerts", True),
		inventory_alert_default_limit=_to_int(_single_value("inventory_alert_default_limit"), 20),
		inventory_alert_critical_ratio=_to_float(_single_value("inventory_alert_critical_ratio"), 0.35),
		inventory_alert_low_ratio=_to_float(_single_value("inventory_alert_low_ratio"), 1.0),
	)
	frappe.local.erpnext_pos_settings_cache = settings
	return settings


def enforce_api_access(*, allow_guest: bool = False) -> POSAPISettings:
	settings = get_settings()
	if not settings.enable_api:
		frappe.throw(
			frappe._("ERPNext POS API is disabled. Enable it in ERPNext POS Settings > Enable API."),
			frappe.PermissionError,
		)
	if not allow_guest and frappe.session.user == "Guest":
		frappe.throw(frappe._("Authentication required"), frappe.AuthenticationError)
	if not allow_guest and frappe.session.user != "Guest":
		current_user = frappe.session.user
		allowed_users = set(settings.allowed_api_users or ())
		if allowed_users and current_user in allowed_users:
			return settings

		required_roles = set(settings.allowed_api_roles or ())
		if required_roles:
			user_roles = set(frappe.get_roles(current_user))
			if not user_roles.intersection(required_roles):
				frappe.throw(
					frappe._("User is missing required role for ERPNext POS API"),
					frappe.PermissionError,
				)
		elif allowed_users and current_user not in allowed_users:
			frappe.throw(
				frappe._("User is not allowed for ERPNext POS API"),
				frappe.PermissionError,
			)
	return settings


def enforce_doctype_permission(doctype: str, ptype: str, doc=None) -> None:
	"""Enforce real ERPNext DocType/doc permission for current API user."""
	current_user = frappe.session.user
	if current_user == "Guest":
		frappe.throw(frappe._("Authentication required"), frappe.AuthenticationError)

	allowed = (
		frappe.has_permission(doc=doc, ptype=ptype, user=current_user)
		if doc is not None
		else frappe.has_permission(doctype=doctype, ptype=ptype, user=current_user)
	)
	if not allowed:
		frappe.throw(
			frappe._("User {0} is missing {1} permission on {2}").format(
				frappe.bold(current_user),
				frappe.bold(ptype),
				frappe.bold(doctype),
			),
			frappe.PermissionError,
		)
