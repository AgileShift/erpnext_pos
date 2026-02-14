from __future__ import annotations

"""Endpoints de usuario para la app POS."""

from typing import Any

import frappe

from .common import ok, standard_api_response
from .settings import enforce_api_access


def _get_doctype_fieldnames(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	return set(frappe.get_all("DocField", filters={"parent": doctype}, pluck="fieldname", page_length=0))


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def get_authenticated_user() -> dict[str, Any]:
	"""Retorna la informacion basica del usuario autenticado."""
	enforce_api_access()
	user = (frappe.session.user or "Guest").strip() or "Guest"
	if user == "Guest":
		frappe.throw("Login required")

	fields_requested = [
		"email",
		"first_name",
		"last_name",
		"username",
		"user_image",
		"language",
		"time_zone",
		"full_name",
	]
	fieldnames = _get_doctype_fieldnames("User")
	fields = [f for f in fields_requested if f in fieldnames]

	row = frappe.get_all("User", filters={"name": user}, fields=fields, limit_page_length=1)
	data = dict(row[0] or {}) if row else {}

	# Normalize and apply fallbacks.
	email = data.get("email") or (user if "@" in user else "")
	first_name = data.get("first_name") or ""
	last_name = data.get("last_name") or ""
	full_name = data.get("full_name") or " ".join([first_name, last_name]).strip()
	username = data.get("username") or user

	return ok(
		{
			"user": user,
			"email": email or None,
			"first_name": first_name or None,
			"last_name": last_name or None,
			"username": username or None,
			"image": data.get("user_image") or None,
			"language": data.get("language") or None,
			"time_zone": data.get("time_zone") or None,
			"full_name": full_name or None,
		}
	)
