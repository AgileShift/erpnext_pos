"""Servicios de configuración centralizada para ERPNext POS v1.

Este módulo expone lectura/escritura del Single `ERPNext POS Settings`,
incluyendo tablas hijas para control de acceso y alertas.
"""

from dataclasses import dataclass
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


SETTINGS_DOCTYPE = "ERPNext POS Settings"
ALLOWED_API_ROLES_CHILD_DOCTYPE = "ERPNext POS API Role"
ALLOWED_API_USERS_CHILD_DOCTYPE = "ERPNext POS API User"
USER_ROLE_BINDINGS_CHILD_DOCTYPE = "ERPNext POS User Role"
INVENTORY_ALERT_RULE_CHILD_DOCTYPE = "ERPNext POS Inventory Alert Rule"


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
	"""Carga configuración efectiva sin depender de permisos de lectura del doctype."""
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


def enforce_doctype_permission(doctype: str, ptype: str, doc=None) -> None:
	"""Valida el permiso real del usuario en el DocType/documento objetivo."""
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


def _clear_settings_cache() -> None:
	if hasattr(frappe.local, "erpnext_pos_settings_cache"):
		delattr(frappe.local, "erpnext_pos_settings_cache")


def _coerce_int(value: Any, default: int) -> int:
	try:
		return int(value)
	except Exception:
		return default


def _coerce_float(value: Any, default: float) -> float:
	try:
		return float(value)
	except Exception:
		return default


def _has_any_key(body: dict[str, Any], *keys: str) -> bool:
	return any(key in body for key in keys)


def _first_key_value(body: dict[str, Any], *keys: str) -> Any:
	for key in keys:
		if key in body:
			return body.get(key)
	return None


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
		candidates = [part.strip() for part in value.split(",")]
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
		name = (candidate or "").strip()
		if not name or name in seen:
			continue
		seen.add(name)
		output.append(name)
	return output


def _ensure_settings_single() -> Any:
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		frappe.throw(f"{SETTINGS_DOCTYPE} DocType is not installed")

	if not frappe.db.exists(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE):
		doc = frappe.get_doc({"doctype": SETTINGS_DOCTYPE, "name": SETTINGS_DOCTYPE})
		doc.insert(ignore_permissions=True)

	return frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


def _get_settings_child_rows(parentfield: str, child_doctype: str, fields: list[str]) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", child_doctype):
		return []

	return frappe.get_all(
		child_doctype,
		filters={
			"parent": SETTINGS_DOCTYPE,
			"parenttype": SETTINGS_DOCTYPE,
			"parentfield": parentfield,
		},
		fields=fields,
		order_by="idx asc",
		page_length=0,
	)


def _build_settings_payload(*, include_options: bool = False) -> dict[str, Any]:
	"""Construye el payload normalizado que consume la app móvil."""
	current = get_settings()
	user_role_bindings = _get_settings_child_rows(
		"user_role_bindings",
		USER_ROLE_BINDINGS_CHILD_DOCTYPE,
		["enabled", "user", "role"],
	)
	inventory_alert_rules = _get_settings_child_rows(
		"inventory_alert_rules",
		INVENTORY_ALERT_RULE_CHILD_DOCTYPE,
		["enabled", "warehouse", "item_group", "critical_ratio", "low_ratio", "priority"],
	)

	data: dict[str, Any] = {
		"enable_api": bool(current.enable_api),
		"allow_discovery": bool(current.allow_discovery),
		"allow_client_secret_response": bool(current.allow_client_secret_response),
		"allowed_api_roles": list(current.allowed_api_roles),
		"allowed_api_users": list(current.allowed_api_users),
		"api_version": current.api_version,
		"default_sync_page_size": int(current.default_sync_page_size),
		"bootstrap_invoice_days": int(current.bootstrap_invoice_days),
		"recent_paid_invoice_days": int(current.recent_paid_invoice_days),
		"enable_inventory_alerts": bool(current.enable_inventory_alerts),
		"inventory_alert_default_limit": int(current.inventory_alert_default_limit),
		"inventory_alert_critical_ratio": float(current.inventory_alert_critical_ratio),
		"inventory_alert_low_ratio": float(current.inventory_alert_low_ratio),
		"user_role_bindings": [
			{
				"enabled": 1 if to_bool(row.get("enabled"), default=True) else 0,
				"user": row.get("user"),
				"role": row.get("role"),
			}
			for row in user_role_bindings
			if row.get("user") and row.get("role")
		],
		"inventory_alert_rules": [
			{
				"enabled": 1 if to_bool(row.get("enabled"), default=True) else 0,
				"warehouse": row.get("warehouse"),
				"item_group": row.get("item_group"),
				"critical_ratio": _coerce_float(row.get("critical_ratio"), 0.35),
				"low_ratio": _coerce_float(row.get("low_ratio"), 1.0),
				"priority": _coerce_int(row.get("priority"), 10),
			}
			for row in inventory_alert_rules
		],
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


def _replace_allowed_api_roles(doc, body: dict[str, Any]) -> None:
	"""Reemplaza roles permitidos para API (tabla + campo legacy CSV)."""
	raw = _first_key_value(body, "allowed_api_roles", "allowedApiRoles", "allowed_api_roles_table")
	roles = _normalize_name_rows(raw, "role")
	for role in roles:
		if not frappe.db.exists("Role", role):
			frappe.throw(f"Role not found: {role}")

	doc.set("allowed_api_roles_table", [])
	for role in roles:
		doc.append("allowed_api_roles_table", {"role": role})
	doc.allowed_api_roles = ",".join(roles)


def _replace_allowed_api_users(doc, body: dict[str, Any]) -> None:
	"""Reemplaza allow-list explícita de usuarios para API protegida."""
	raw = _first_key_value(body, "allowed_api_users", "allowedApiUsers")
	users = _normalize_name_rows(raw, "user")
	for user in users:
		if not frappe.db.exists("User", user):
			frappe.throw(f"User not found: {user}")

	doc.set("allowed_api_users", [])
	for user in users:
		doc.append("allowed_api_users", {"user": user})


def _replace_user_role_bindings(doc, body: dict[str, Any]) -> None:
	"""Reemplaza asignaciones dinámicas usuario->rol desde configuración central."""
	rows = _as_list(_first_key_value(body, "user_role_bindings", "userRoleBindings"))
	doc.set("user_role_bindings", [])
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		user = str(value_from_aliases(raw, "user", default="") or "").strip()
		role = str(value_from_aliases(raw, "role", default="") or "").strip()
		if not user or not role:
			continue
		if not frappe.db.exists("User", user):
			frappe.throw(f"User not found: {user}")
		if not frappe.db.exists("Role", role):
			frappe.throw(f"Role not found: {role}")
		doc.append(
			"user_role_bindings",
			{
				"enabled": 1 if to_bool(value_from_aliases(raw, "enabled"), default=True) else 0,
				"user": user,
				"role": role,
			},
		)


def _replace_inventory_alert_rules(doc, body: dict[str, Any]) -> None:
	"""Reemplaza reglas de alertas de inventario por bodega/grupo."""
	rows = _as_list(_first_key_value(body, "inventory_alert_rules", "inventoryAlertRules"))
	doc.set("inventory_alert_rules", [])
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		warehouse = str(value_from_aliases(raw, "warehouse", default="") or "").strip()
		item_group = str(value_from_aliases(raw, "item_group", "itemGroup", default="") or "").strip()

		if warehouse and not frappe.db.exists("Warehouse", warehouse):
			frappe.throw(f"Warehouse not found: {warehouse}")
		if item_group and not frappe.db.exists("Item Group", item_group):
			frappe.throw(f"Item Group not found: {item_group}")

		critical_ratio = _coerce_float(value_from_aliases(raw, "critical_ratio", "criticalRatio"), 0.35)
		low_ratio = _coerce_float(value_from_aliases(raw, "low_ratio", "lowRatio"), 1.0)
		if critical_ratio < 0:
			critical_ratio = 0.0
		if low_ratio < critical_ratio:
			low_ratio = critical_ratio
		priority = _coerce_int(value_from_aliases(raw, "priority"), 10)
		if priority < 0:
			priority = 0

		doc.append(
			"inventory_alert_rules",
			{
				"enabled": 1 if to_bool(value_from_aliases(raw, "enabled"), default=True) else 0,
				"warehouse": warehouse or None,
				"item_group": item_group or None,
				"critical_ratio": critical_ratio,
				"low_ratio": low_ratio,
				"priority": priority,
			},
		)


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def mobile_get(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	"""Endpoint protegido para consultar configuración central POS."""
	enforce_doctype_permission(SETTINGS_DOCTYPE, "read")
	body = parse_payload(payload)
	include_options = to_bool(value_from_aliases(body, "include_options", "includeOptions"), default=False)
	_ensure_settings_single()
	_clear_settings_cache()
	return ok(_build_settings_payload(include_options=include_options))


@frappe.whitelist(methods=["POST"])
@standard_api_response
def mobile_update(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
	"""Endpoint protegido para actualizar configuración central POS con idempotencia."""
	enforce_doctype_permission(SETTINGS_DOCTYPE, "write")
	body = parse_payload(payload)
	settings_body = body.get("settings") if isinstance(body.get("settings"), dict) else body

	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(settings_body, "client_request_id", "clientRequestId", default="") or ""),
		settings_body,
	)
	endpoint = "settings.mobile_update"
	request_hash_value = payload_hash(settings_body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc = _ensure_settings_single()

	bool_fields = {
		"enable_api": ("enable_api", "enableApi"),
		"allow_discovery": ("allow_discovery", "allowDiscovery"),
		"allow_client_secret_response": ("allow_client_secret_response", "allowClientSecretResponse"),
		"enable_inventory_alerts": ("enable_inventory_alerts", "enableInventoryAlerts"),
	}
	for fieldname, aliases in bool_fields.items():
		if _has_any_key(settings_body, *aliases):
			doc.set(fieldname, 1 if to_bool(_first_key_value(settings_body, *aliases), default=False) else 0)

	int_fields = {
		"default_sync_page_size": ("default_sync_page_size", "defaultSyncPageSize"),
		"bootstrap_invoice_days": ("bootstrap_invoice_days", "bootstrapInvoiceDays"),
		"recent_paid_invoice_days": ("recent_paid_invoice_days", "recentPaidInvoiceDays"),
		"inventory_alert_default_limit": ("inventory_alert_default_limit", "inventoryAlertDefaultLimit"),
	}
	for fieldname, aliases in int_fields.items():
		if _has_any_key(settings_body, *aliases):
			doc.set(fieldname, _coerce_int(_first_key_value(settings_body, *aliases), int(doc.get(fieldname) or 0)))

	float_fields = {
		"inventory_alert_critical_ratio": ("inventory_alert_critical_ratio", "inventoryAlertCriticalRatio"),
		"inventory_alert_low_ratio": ("inventory_alert_low_ratio", "inventoryAlertLowRatio"),
	}
	for fieldname, aliases in float_fields.items():
		if _has_any_key(settings_body, *aliases):
			doc.set(fieldname, _coerce_float(_first_key_value(settings_body, *aliases), float(doc.get(fieldname) or 0)))

	if _has_any_key(settings_body, "allowed_api_roles", "allowedApiRoles", "allowed_api_roles_table"):
		_replace_allowed_api_roles(doc, settings_body)
	if _has_any_key(settings_body, "allowed_api_users", "allowedApiUsers"):
		_replace_allowed_api_users(doc, settings_body)
	if _has_any_key(settings_body, "user_role_bindings", "userRoleBindings"):
		_replace_user_role_bindings(doc, settings_body)
	if _has_any_key(settings_body, "inventory_alert_rules", "inventoryAlertRules"):
		_replace_inventory_alert_rules(doc, settings_body)

	doc.save(ignore_permissions=True)
	_clear_settings_cache()
	include_options = to_bool(value_from_aliases(body, "include_options", "includeOptions"), default=False)
	result = _build_settings_payload(include_options=include_options)
	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_name=SETTINGS_DOCTYPE,
	)
	return ok(result, request_id=request_id)
