from __future__ import annotations

import frappe

from .v1.settings import enforce_api_access, get_settings


def enforce_api_guard() -> None:
	"""Global request guard for all erpnext_pos v1 RPC endpoints."""
	cmd = frappe.form_dict.get("cmd")
	if not isinstance(cmd, str):
		return
	if not cmd.startswith("erpnext_pos.api.v1."):
		return

	if cmd == "erpnext_pos.api.v1.discovery.resolve_site":
		settings = get_settings()
		if not settings.enable_api:
			frappe.throw(
				frappe._("ERPNext POS API is disabled. Enable it in ERPNext POS Settings > Enable API."),
				frappe.PermissionError,
			)
		if not settings.allow_discovery:
			frappe.throw(frappe._("Discovery endpoint is disabled"), frappe.PermissionError)
		return

	enforce_api_access()
