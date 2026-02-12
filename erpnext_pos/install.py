from __future__ import annotations

import json

import frappe

from .access import bootstrap_access_controls

SETTINGS_DOCTYPE = "ERPNext POS Settings"
WORKSPACE_DOCTYPE = "Workspace"
MODULE_DEF_DOCTYPE = "Module Def"
POS_MODULE_NAME = "ERPNext POS"
POS_WORKSPACE_NAME = "POS Mobile"
LEGACY_WORKSPACE_NAME = "ERPNext POS"
SETTINGS_DEFAULTS = {
	"enable_api": 1,
	"allow_discovery": 1,
	"allow_client_secret_response": 0,
	"allowed_api_roles": "System Manager,POS,POS User",
	"api_version": "v1",
	"default_sync_page_size": 50,
	"bootstrap_invoice_days": 90,
	"recent_paid_invoice_days": 7,
	"enable_inventory_alerts": 1,
	"inventory_alert_default_limit": 20,
	"inventory_alert_critical_ratio": 0.35,
	"inventory_alert_low_ratio": 1.0,
}
POS_WORKSPACE_CONTENT = json.dumps(
	[
		{
			"id": "pm_header",
			"type": "header",
			"data": {"text": "<span class=\"h4\"><b>POS Mobile Setup</b></span>", "col": 12},
		},
		{
			"id": "pm_settings_shortcut",
			"type": "shortcut",
			"data": {"shortcut_name": "ERPNext POS Settings", "col": 3},
		},
		{
			"id": "pm_card_config",
			"type": "card",
			"data": {"card_name": "POS Mobile Configuration", "col": 4},
		},
	],
	separators=(",", ":"),
)

POS_WORKSPACE_LINKS = [
	{
		"type": "Card Break",
		"label": "POS Mobile Configuration",
		"link_count": 1,
		"is_query_report": 0,
		"hidden": 0,
		"onboard": 0,
	},
	{
		"type": "Link",
		"label": "ERPNext POS Settings",
		"link_to": "ERPNext POS Settings",
		"link_type": "DocType",
		"is_query_report": 0,
		"hidden": 0,
		"onboard": 0,
	},
]

POS_WORKSPACE_SHORTCUTS = [
	{
		"label": "ERPNext POS Settings",
		"link_to": "ERPNext POS Settings",
		"type": "DocType",
		"doc_view": "",
		"color": "Grey",
	}
]

DEFAULT_POS_WORKSPACE_ROLES = ("System Manager", "POS", "POS User")
VALID_SHORTCUT_DOC_VIEWS = {"", "List", "Report Builder", "Dashboard", "Tree", "New", "Calendar", "Kanban", "Image"}


def _ensure_settings_single_defaults() -> None:
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return

	available_fields = set(
		frappe.get_all("DocField", filters={"parent": SETTINGS_DOCTYPE}, pluck="fieldname", page_length=0)
	)
	for fieldname, default_value in SETTINGS_DEFAULTS.items():
		if fieldname not in available_fields:
			continue
		current = frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
		if current is None or (isinstance(current, str) and not current.strip()):
			frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, default_value)


def _ensure_module_def() -> None:
	if not frappe.db.exists("DocType", MODULE_DEF_DOCTYPE):
		return

	if not frappe.db.exists(MODULE_DEF_DOCTYPE, POS_MODULE_NAME):
		module = frappe.get_doc(
			{
				"doctype": MODULE_DEF_DOCTYPE,
				"module_name": POS_MODULE_NAME,
				"app_name": "erpnext_pos",
			}
		)
		module.insert(ignore_permissions=True)


def _sanitize_workspace_shortcuts(workspace) -> None:
	for shortcut in workspace.get("shortcuts") or []:
		doc_view = shortcut.get("doc_view") or ""
		if doc_view not in VALID_SHORTCUT_DOC_VIEWS:
			shortcut.doc_view = ""


def _get_pos_workspace_roles() -> list[dict[str, str]]:
	if not frappe.db.exists("DocType", "Role"):
		return []
	existing = set(
		frappe.get_all("Role", filters={"name": ["in", list(DEFAULT_POS_WORKSPACE_ROLES)]}, pluck="name", page_length=0)
	)
	ordered_roles = [role for role in DEFAULT_POS_WORKSPACE_ROLES if role in existing]
	return [{"role": role} for role in ordered_roles]


def _replace_workspace_children(workspace, workspace_roles: list[dict[str, str]]) -> None:
	workspace.set("links", [])
	for row in POS_WORKSPACE_LINKS:
		workspace.append("links", row)

	workspace.set("shortcuts", [])
	for row in POS_WORKSPACE_SHORTCUTS:
		workspace.append("shortcuts", row)

	workspace.set("roles", [])
	for row in workspace_roles:
		workspace.append("roles", row)

	_sanitize_workspace_shortcuts(workspace)

def _hide_legacy_workspace() -> None:
	if not frappe.db.exists("DocType", WORKSPACE_DOCTYPE):
		return
	if not frappe.db.exists(WORKSPACE_DOCTYPE, LEGACY_WORKSPACE_NAME):
		return

	legacy = frappe.get_doc(WORKSPACE_DOCTYPE, LEGACY_WORKSPACE_NAME)
	_sanitize_workspace_shortcuts(legacy)
	if not legacy.is_hidden:
		legacy.is_hidden = 1
		legacy.save(ignore_permissions=True)


def after_install():
	"""Initialize defaults for required singleton after app installation."""
	_ensure_settings_single_defaults()
	_ensure_module_def()
	_hide_legacy_workspace()
	bootstrap_access_controls()


def after_migrate():
	"""Backfill singleton defaults for existing sites on migrate."""
	_ensure_settings_single_defaults()
	_ensure_module_def()
	_ensure_pos_mobile_workspace()
	_hide_legacy_workspace()
	bootstrap_access_controls()
