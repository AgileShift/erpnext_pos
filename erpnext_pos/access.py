from __future__ import annotations

"""Orquestación de controles de acceso para ERPNext POS.

Responsabilidades:
- leer configuración del Single `ERPNext POS Settings`
- asignar roles a usuarios según tabla de bindings
- mantener fallback mínimo para que POS móvil pueda operar
"""

from collections.abc import Iterable
from typing import Any

import frappe
import frappe.permissions


SETTINGS_DOCTYPE = "ERPNext POS Settings"

ALLOWED_API_ROLES_FIELD = "allowed_api_roles_table"
ALLOWED_API_ROLES_CHILD_DOCTYPE = "ERPNext POS API Role"
ALLOWED_API_USERS_FIELD = "allowed_api_users"
ALLOWED_API_USERS_CHILD_DOCTYPE = "ERPNext POS API User"
USER_ROLE_BINDINGS_FIELD = "user_role_bindings"
USER_ROLE_BINDINGS_CHILD_DOCTYPE = "ERPNext POS User Role"

DEFAULT_ALLOWED_API_ROLES = ("System Manager", "POS", "POS User")
DEFAULT_ASSIGNABLE_ROLE_ORDER = ("POS User", "POS")

# Compatibility defaults for mobile users that still read these doctypes directly.
MOBILE_READ_PERMISSION_MATRIX: dict[str, tuple[str, ...]] = {
	"Item": ("read", "select", "report"),
	"Item Price": ("read", "select", "report"),
	"Bin": ("read", "select", "report"),
}
DOC_PERM_FIELDS = (
	"select",
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
)


def _to_bool(value: Any, *, default: bool = False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	text = str(value).strip().lower()
	if not text:
		return default
	if text in {"1", "true", "yes", "y", "on"}:
		return True
	if text in {"0", "false", "no", "n", "off"}:
		return False
	return default


def _to_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except Exception:
		return default


def _normalize_names(values: Iterable[Any]) -> tuple[str, ...]:
	result: list[str] = []
	seen: set[str] = set()
	for value in values:
		name = str(value or "").strip()
		if not name or name in seen:
			continue
		seen.add(name)
		result.append(name)
	return tuple(result)


def _merge_names(*groups: Iterable[Any]) -> tuple[str, ...]:
	merged: list[Any] = []
	for group in groups:
		merged.extend(list(group or ()))
	return _normalize_names(merged)


def _field_exists(doctype: str, fieldname: str) -> bool:
	return bool(frappe.db.exists("DocField", {"parent": doctype, "fieldname": fieldname}))


def _child_table_exists(child_doctype: str, parent_field: str) -> bool:
	return frappe.db.exists("DocType", child_doctype) and _field_exists(SETTINGS_DOCTYPE, parent_field)


def _get_settings_doc():
	try:
		return frappe.get_doc(SETTINGS_DOCTYPE)
	except Exception:
		return None


def _existing_roles(roles: Iterable[Any]) -> tuple[str, ...]:
	role_names = _normalize_names(roles)
	if not role_names:
		return ()
	existing = set(
		frappe.get_all("Role", filters={"name": ["in", list(role_names)]}, pluck="name", page_length=0)
	)
	return tuple(role for role in role_names if role in existing)


def _parse_roles_csv(value: str | None) -> tuple[str, ...]:
	if not value:
		return ()
	return _normalize_names(value.split(","))


def _get_child_rows(
	settings_doc,
	parent_field: str,
	child_doctype: str,
	fields: tuple[str, ...],
) -> list[dict[str, Any]]:
	if settings_doc and hasattr(settings_doc, parent_field):
		rows = []
		for row in settings_doc.get(parent_field) or []:
			if hasattr(row, "get"):
				rows.append({field: row.get(field) for field in fields})
			else:
				rows.append({field: getattr(row, field, None) for field in fields})
		return rows

	if not _child_table_exists(child_doctype, parent_field):
		return []

	return frappe.get_all(
		child_doctype,
		filters={
			"parent": SETTINGS_DOCTYPE,
			"parenttype": SETTINGS_DOCTYPE,
			"parentfield": parent_field,
		},
		fields=list(fields),
		page_length=0,
	)


def get_configured_allowed_roles(settings_doc=None) -> tuple[str, ...]:
	"""Obtiene roles permitidos para la API combinando tabla y fallback legacy."""
	roles_from_table = _normalize_names(
		row.get("role")
		for row in _get_child_rows(
			settings_doc,
			ALLOWED_API_ROLES_FIELD,
			ALLOWED_API_ROLES_CHILD_DOCTYPE,
			("role",),
		)
	)

	roles_from_legacy_csv = ()
	if settings_doc and hasattr(settings_doc, "allowed_api_roles"):
		roles_from_legacy_csv = _parse_roles_csv(settings_doc.get("allowed_api_roles"))
	elif _field_exists(SETTINGS_DOCTYPE, "allowed_api_roles"):
		roles_from_legacy_csv = _parse_roles_csv(
			frappe.db.get_single_value(SETTINGS_DOCTYPE, "allowed_api_roles", cache=False)
		)

	roles = _merge_names(roles_from_table, roles_from_legacy_csv)
	if not roles:
		roles = DEFAULT_ALLOWED_API_ROLES
	return _existing_roles(roles)


def ensure_default_allowed_api_roles() -> None:
	"""Seed role table so roles are selected from Link API instead of csv typing."""
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return
	if not _child_table_exists(ALLOWED_API_ROLES_CHILD_DOCTYPE, ALLOWED_API_ROLES_FIELD):
		return

	try:
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
	except Exception:
		return

	existing_rows = settings.get(ALLOWED_API_ROLES_FIELD) or []
	if existing_rows:
		return

	roles = get_configured_allowed_roles(settings)
	for role in roles:
		settings.append(ALLOWED_API_ROLES_FIELD, {"role": role})

	if roles:
		settings.save(ignore_permissions=True)


def get_configured_allowed_users(settings_doc=None) -> tuple[str, ...]:
	"""Obtiene usuarios explícitamente autorizados para la API."""
	rows = _get_child_rows(
		settings_doc,
		ALLOWED_API_USERS_FIELD,
		ALLOWED_API_USERS_CHILD_DOCTYPE,
		("user",),
	)
	return _normalize_names(row.get("user") for row in rows)


def get_configured_user_role_bindings(settings_doc=None) -> list[tuple[str, str]]:
	"""Lee asignaciones usuario->rol activas definidas en settings."""
	rows = _get_child_rows(
		settings_doc,
		USER_ROLE_BINDINGS_FIELD,
		USER_ROLE_BINDINGS_CHILD_DOCTYPE,
		("enabled", "user", "role"),
	)
	bindings: list[tuple[str, str]] = []
	for row in rows:
		if not _to_bool(row.get("enabled"), default=True):
			continue
		user = str(row.get("user") or "").strip()
		role = str(row.get("role") or "").strip()
		if not user or not role:
			continue
		bindings.append((user, role))
	return bindings


def ensure_default_allowed_api_users() -> None:
	"""Seed allowed users with admin and users that already have configured POS roles."""
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return
	if not _child_table_exists(ALLOWED_API_USERS_CHILD_DOCTYPE, ALLOWED_API_USERS_FIELD):
		return

	try:
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
	except Exception:
		return

	existing = set(get_configured_allowed_users(settings))
	if existing:
		return

	candidates = {"Administrator"}
	roles = get_configured_allowed_roles(settings)
	if roles and frappe.db.exists("DocType", "Has Role"):
		role_rows = frappe.get_all(
			"Has Role",
			filters={"role": ["in", list(roles)], "parenttype": "User"},
			fields=["parent"],
			page_length=0,
		)
		for row in role_rows:
			user = (row.get("parent") or "").strip()
			if user and user != "Guest" and frappe.db.exists("User", user):
				candidates.add(user)

	for user, _ in get_configured_user_role_bindings(settings):
		if user != "Guest" and frappe.db.exists("User", user):
			candidates.add(user)

	for user in sorted(candidates):
		settings.append(ALLOWED_API_USERS_FIELD, {"user": user})

	settings.save(ignore_permissions=True)


def apply_user_role_bindings(bindings: list[tuple[str, str]]) -> None:
	"""Asigna roles faltantes a usuarios según bindings configurados."""
	if not bindings:
		return

	valid_roles = set(_existing_roles(role for _, role in bindings))
	if not valid_roles:
		return

	users_to_roles: dict[str, set[str]] = {}
	for user, role in bindings:
		if role not in valid_roles:
			continue
		if user in {"Guest", "Administrator"} or not frappe.db.exists("User", user):
			continue
		users_to_roles.setdefault(user, set()).add(role)

	for user, roles in users_to_roles.items():
		user_roles = set(frappe.get_roles(user))
		missing = sorted(role for role in roles if role not in user_roles)
		if not missing:
			continue
		user_doc = frappe.get_doc("User", user)
		user_doc.add_roles(*missing)


def _upsert_custom_docperm(
	target_doctype: str,
	role: str,
	permlevel: int,
	if_owner: int,
	permissions: dict[str, bool],
) -> None:
	frappe.permissions.setup_custom_perms(target_doctype)

	existing_name = frappe.db.get_value(
		"Custom DocPerm",
		{
			"parent": target_doctype,
			"role": role,
			"permlevel": permlevel,
			"if_owner": if_owner,
		},
	)
	if existing_name:
		doc = frappe.get_doc("Custom DocPerm", existing_name)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": target_doctype,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": role,
				"permlevel": permlevel,
				"if_owner": if_owner,
			}
		)
		doc.insert(ignore_permissions=True)

	for ptype in DOC_PERM_FIELDS:
		doc.set(ptype, 1 if permissions.get(ptype) else 0)
	doc.save(ignore_permissions=True)


def apply_permission_rules(rules: list[dict[str, Any]]) -> None:
	"""Aplica reglas configuradas en settings sobre `Custom DocPerm` del core."""
	if not rules:
		return

	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype

	valid_roles = set(_existing_roles(rule.get("role") for rule in rules))
	touched_doctypes: set[str] = set()

	for rule in rules:
		target_doctype = str(rule.get("target_doctype") or "").strip()
		role = str(rule.get("role") or "").strip()
		if not target_doctype or role not in valid_roles:
			continue
		if not frappe.db.exists("DocType", target_doctype):
			continue

		permissions = {ptype: _to_bool(rule.get(ptype)) for ptype in DOC_PERM_FIELDS}
		if any(
			permissions[ptype]
			for ptype in DOC_PERM_FIELDS
			if ptype not in {"read", "select"}
		):
			permissions["read"] = True

		_upsert_custom_docperm(
			target_doctype=target_doctype,
			role=role,
			permlevel=max(_to_int(rule.get("permlevel"), 0), 0),
			if_owner=1 if _to_bool(rule.get("if_owner")) else 0,
			permissions=permissions,
		)
		touched_doctypes.add(target_doctype)

	for target_doctype in touched_doctypes:
		validate_permissions_for_doctype(target_doctype)
		frappe.clear_cache(doctype=target_doctype)


def ensure_fallback_mobile_permissions(roles: Iterable[str]) -> None:
	"""Garantiza permisos mínimos de lectura para operación de la app móvil."""
	role_names = _existing_roles(roles)
	if not role_names:
		return

	target_doctypes = [
		doctype for doctype in MOBILE_READ_PERMISSION_MATRIX.keys() if frappe.db.exists("DocType", doctype)
	]
	if not target_doctypes:
		return

	existing_rows = frappe.get_all(
		"Custom DocPerm",
		filters={
			"parent": ["in", target_doctypes],
			"role": ["in", list(role_names)],
			"permlevel": 0,
			"if_owner": 0,
		},
		fields=["parent", "role"],
		page_length=0,
	)
	existing_keys = {(row.get("parent"), row.get("role")) for row in existing_rows}

	rules: list[dict[str, Any]] = []
	for role in role_names:
		for target_doctype, perm_types in MOBILE_READ_PERMISSION_MATRIX.items():
			if target_doctype not in target_doctypes:
				continue
			if (target_doctype, role) in existing_keys:
				continue
			row = {
				"target_doctype": target_doctype,
				"role": role,
				"permlevel": 0,
				"if_owner": 0,
			}
			for ptype in DOC_PERM_FIELDS:
				row[ptype] = ptype in perm_types
			rules.append(row)
	apply_permission_rules(rules)


def ensure_allowed_users_have_api_role(users: Iterable[str], roles: Iterable[str]) -> None:
	"""Garantiza que cada usuario permitido tenga al menos un rol habilitado para API."""
	role_names = _existing_roles(roles)
	if not role_names:
		return

	assignable_roles = tuple(role for role in role_names if role != "System Manager")
	if not assignable_roles:
		return

	assign_role = next(
		(role for role in DEFAULT_ASSIGNABLE_ROLE_ORDER if role in assignable_roles),
		assignable_roles[0],
	)

	for user in _normalize_names(users):
		if user in {"Guest", "Administrator"}:
			continue
		if not frappe.db.exists("User", user):
			continue

		user_roles = set(frappe.get_roles(user))
		if user_roles.intersection(role_names):
			continue

		user_doc = frappe.get_doc("User", user)
		user_doc.add_roles(assign_role)


def apply_settings_access_controls(settings_doc=None) -> None:
	"""Aplica en el core (roles de usuarios) la configuración central del POS."""
	roles = get_configured_allowed_roles(settings_doc)
	users = get_configured_allowed_users(settings_doc)
	role_bindings = get_configured_user_role_bindings(settings_doc)

	if role_bindings:
		apply_user_role_bindings(role_bindings)
		roles = _merge_names(roles, (role for _, role in role_bindings))

	ensure_allowed_users_have_api_role(users, roles)


def bootstrap_access_controls() -> None:
	"""Inicialización segura para instalaciones nuevas o migradas."""
	ensure_default_allowed_api_roles()
	ensure_default_allowed_api_users()
	settings_doc = _get_settings_doc()
	roles = get_configured_allowed_roles(settings_doc)
	role_bindings = get_configured_user_role_bindings(settings_doc)
	if role_bindings:
		roles = _merge_names(roles, (role for _, role in role_bindings))
	ensure_fallback_mobile_permissions(roles)
	apply_settings_access_controls(settings_doc)
