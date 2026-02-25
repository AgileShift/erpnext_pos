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

	settings = POSAPISettings(
		default_sync_page_size=5050,
		bootstrap_invoice_days=90,
		recent_paid_invoice_days=7,
		enable_inventory_alerts=True
	)
	frappe.local.erpnext_pos_settings_cache = settings
	return settings


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


def _build_settings_payload(*, include_options: bool = False) -> dict[str, Any]:
	"""Construye el payload normalizado que consume la app móvil."""
	current = get_settings()

	data: dict[str, Any] = {
		"allow_client_secret_response": bool(current.allow_client_secret_response),
		"default_sync_page_size": int(current.default_sync_page_size),
		"bootstrap_invoice_days": int(current.bootstrap_invoice_days),
		"recent_paid_invoice_days": int(current.recent_paid_invoice_days),
		"enable_inventory_alerts": bool(current.enable_inventory_alerts),
		"inventory_alert_default_limit": int(current.inventory_alert_default_limit),
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

	body = parse_payload(payload)
	include_options = to_bool(value_from_aliases(body, "include_options", "includeOptions"), default=False)

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


	doc.save(ignore_permissions=True)

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
