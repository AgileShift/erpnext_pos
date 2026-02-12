from __future__ import annotations

from typing import Any
import json

import frappe
from frappe import _
from frappe.core.doctype.custom_docperm.custom_docperm import update_custom_docperm
from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype
from frappe.permissions import setup_custom_perms

from .access import DOC_PERM_FIELDS, get_doctype_permission_rows, sync_settings_permission_rules_from_custom


SETTINGS_DOCTYPE = "ERPNext POS Settings"


def _to_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_int(value: Any) -> int:
	try:
		return int(value)
	except Exception:
		return 0


def _assert_can_manage_permissions() -> None:
	if frappe.session.user == "Guest":
		frappe.throw(_("Authentication required"), frappe.AuthenticationError)
	if not frappe.has_permission(SETTINGS_DOCTYPE, "write", user=frappe.session.user):
		frappe.throw(_("You are not allowed to manage POS permissions"), frappe.PermissionError)


def _validate_inputs(doctype: str, role: str | None = None) -> tuple[str, str]:
	target_doctype = (doctype or "").strip()
	if not target_doctype:
		frappe.throw(_("doctype is required"))
	if not frappe.db.exists("DocType", target_doctype):
		frappe.throw(_("DocType {0} not found").format(target_doctype))

	target_role = (role or "").strip()
	if target_role and not frappe.db.exists("Role", target_role):
		frappe.throw(_("Role {0} not found").format(target_role))
	return target_doctype, target_role


def _normalize_rights(rights: Any) -> dict[str, int]:
	if isinstance(rights, str):
		text = rights.strip()
		rights = json.loads(text) if text else {}
	if not isinstance(rights, dict):
		rights = {}

	normalized = {ptype: 1 if _to_bool(rights.get(ptype)) else 0 for ptype in DOC_PERM_FIELDS}
	if any(normalized[ptype] for ptype in DOC_PERM_FIELDS if ptype not in {"read", "select"}):
		normalized["read"] = 1
	return normalized


def _get_custom_docperm_name(doctype: str, role: str, permlevel: int, if_owner: int) -> str | None:
	return frappe.db.get_value(
		"Custom DocPerm",
		{
			"parent": doctype,
			"role": role,
			"permlevel": permlevel,
			"if_owner": if_owner,
		},
	)


def _get_or_create_custom_docperm_name(doctype: str, role: str, permlevel: int, if_owner: int) -> str:
	setup_custom_perms(doctype)
	existing_name = _get_custom_docperm_name(doctype, role, permlevel, if_owner)
	if existing_name:
		return existing_name

	doc = frappe.get_doc(
		{
			"doctype": "Custom DocPerm",
			"parent": doctype,
			"parenttype": "DocType",
			"parentfield": "permissions",
			"role": role,
			"permlevel": permlevel,
			"if_owner": if_owner,
			"read": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _save_custom_docperm_rights(
	doctype: str,
	role: str,
	permlevel: int,
	if_owner: int,
	rights: dict[str, int],
) -> str:
	custom_docperm_name = _get_or_create_custom_docperm_name(doctype, role, permlevel, if_owner)
	updates: dict[str, int] = {"if_owner": 1 if if_owner else 0}
	for ptype in DOC_PERM_FIELDS:
		updates[ptype] = 1 if rights.get(ptype) else 0
	# Frappe core: report is not valid together with if_owner.
	if updates["if_owner"] and updates.get("report"):
		updates["report"] = 0

	update_custom_docperm(custom_docperm_name, updates)
	validate_permissions_for_doctype(doctype)
	frappe.clear_cache(doctype=doctype)
	sync_settings_permission_rules_from_custom(doctype)
	return custom_docperm_name


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
def get_permission_matrix(doctype: str, role: str | None = None) -> dict[str, Any]:
	_assert_can_manage_permissions()
	target_doctype, target_role = _validate_inputs(doctype, role)
	rows = get_doctype_permission_rows(target_doctype, target_role or None)
	return {
		"doctype": target_doctype,
		"role": target_role or None,
		"rows": rows,
		"rights": list(DOC_PERM_FIELDS),
	}


@frappe.whitelist(methods=["POST"])
def add_matrix_rule(
	doctype: str,
	role: str,
	permlevel: int = 0,
	if_owner: int = 0,
) -> dict[str, Any]:
	_assert_can_manage_permissions()
	target_doctype, target_role = _validate_inputs(doctype, role)
	permlevel_value = max(_to_int(permlevel), 0)
	if_owner_value = 1 if _to_bool(if_owner) else 0
	existing_name = _get_custom_docperm_name(
		target_doctype,
		target_role,
		permlevel_value,
		if_owner_value,
	)
	if existing_name:
		frappe.throw(
			_(
				"A rule already exists for this Role, Perm Level and owner mode. Edit it in the matrix instead."
			),
			title=_("Rule Exists"),
		)
	_save_custom_docperm_rights(
		doctype=target_doctype,
		role=target_role,
		permlevel=permlevel_value,
		if_owner=if_owner_value,
		rights={"read": 1},
	)
	return get_permission_matrix(target_doctype)


@frappe.whitelist(methods=["POST"])
def update_matrix_rule(
	doctype: str,
	role: str,
	permlevel: int,
	if_owner: int,
	ptype: str,
	value: int | str | bool | None = None,
) -> dict[str, Any]:
	_assert_can_manage_permissions()
	target_doctype, target_role = _validate_inputs(doctype, role)

	ptype_name = (ptype or "").strip()
	allowed_ptypes = set(DOC_PERM_FIELDS) | {"if_owner"}
	if ptype_name not in allowed_ptypes:
		frappe.throw(_("Unsupported permission type: {0}").format(ptype_name))

	permlevel_value = max(_to_int(permlevel), 0)
	current_if_owner = 1 if _to_bool(if_owner) else 0
	new_value = 1 if _to_bool(value) else 0

	if ptype_name == "report" and new_value and current_if_owner:
		frappe.throw(_("Cannot set Report permission when Only If Creator is enabled"))

	current_name = _get_or_create_custom_docperm_name(
		target_doctype,
		target_role,
		permlevel_value,
		current_if_owner,
	)
	if ptype_name == "if_owner":
		target_if_owner = new_value
		if target_if_owner != current_if_owner:
			conflict = _get_custom_docperm_name(
				target_doctype,
				target_role,
				permlevel_value,
				target_if_owner,
			)
			if conflict:
				frappe.throw(
					_(
						"A rule with the same Role and Perm Level already exists for the selected owner mode"
					)
				)
			updates = {"if_owner": target_if_owner}
			if target_if_owner:
				updates["report"] = 0
			update_custom_docperm(current_name, updates)
	else:
		updates = {ptype_name: new_value}
		if ptype_name != "read" and new_value:
			updates["read"] = 1
		if ptype_name == "report" and current_if_owner:
			updates["report"] = 0
		update_custom_docperm(current_name, updates)

	validate_permissions_for_doctype(target_doctype)
	frappe.clear_cache(doctype=target_doctype)
	sync_settings_permission_rules_from_custom(target_doctype)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def remove_matrix_rule(
	doctype: str,
	role: str,
	permlevel: int,
	if_owner: int = 0,
) -> dict[str, Any]:
	_assert_can_manage_permissions()
	target_doctype, target_role = _validate_inputs(doctype, role)
	permlevel_value = max(_to_int(permlevel), 0)
	if_owner_value = 1 if _to_bool(if_owner) else 0

	setup_custom_perms(target_doctype)
	names = frappe.get_all(
		"Custom DocPerm",
		filters={
			"parent": target_doctype,
			"role": target_role,
			"permlevel": permlevel_value,
			"if_owner": if_owner_value,
		},
		pluck="name",
		page_length=0,
	)
	for name in names:
		frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True, force=True)

	if not frappe.get_all("Custom DocPerm", filters={"parent": target_doctype}, pluck="name", limit=1):
		frappe.throw(_("There must be at least one permission rule"), title=_("Cannot Remove"))

	validate_permissions_for_doctype(target_doctype, for_remove=True, alert=True)
	frappe.clear_cache(doctype=target_doctype)
	sync_settings_permission_rules_from_custom(target_doctype)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def set_matrix_rule(
	doctype: str,
	role: str,
	permlevel: int = 0,
	if_owner: int = 0,
	rights: Any = None,
) -> dict[str, Any]:
	"""Save a full permission row atomically (used by matrix UI)."""
	_assert_can_manage_permissions()
	target_doctype, target_role = _validate_inputs(doctype, role)
	permlevel_value = max(_to_int(permlevel), 0)
	if_owner_value = 1 if _to_bool(if_owner) else 0
	normalized_rights = _normalize_rights(rights)
	if if_owner_value:
		normalized_rights["report"] = 0

	name = _save_custom_docperm_rights(
		doctype=target_doctype,
		role=target_role,
		permlevel=permlevel_value,
		if_owner=if_owner_value,
		rights=normalized_rights,
	)
	return {"ok": True, "name": name}


@frappe.whitelist(methods=["POST"])
def sync_matrix_to_pos_settings(doctype: str) -> dict[str, Any]:
	_assert_can_manage_permissions()
	target_doctype, _ = _validate_inputs(doctype)
	inserted = sync_settings_permission_rules_from_custom(target_doctype)
	return {"ok": True, "inserted": inserted}
