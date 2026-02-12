from __future__ import annotations

"""Guard global de seguridad para endpoints `erpnext_pos.api.v1.*`."""

import frappe

from .v1.settings import enforce_api_access, get_settings


def enforce_api_guard() -> None:
	"""Valida habilitación y autenticación antes de ejecutar endpoints v1."""
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
