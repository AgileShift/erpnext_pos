from __future__ import annotations

from typing import Any

import frappe
from frappe.utils.data import get_url

from .common import ok, standard_api_response
from .settings import enforce_api_access


def _get_doctype_fieldnames(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	return set(frappe.get_all("DocField", filters={"parent": doctype}, pluck="fieldname", page_length=0))


def _get_runtime_defaults(platform_key: str) -> dict[str, Any]:
	profile_fields_available = _get_doctype_fieldnames("POS Profile")
	profile_fields = ["name"]
	for fieldname in ("company", "warehouse", "currency", "selling_price_list"):
		if fieldname in profile_fields_available:
			profile_fields.append(fieldname)

	profile_row = frappe.get_all(
		"POS Profile",
		filters={"disabled": 0},
		fields=profile_fields,
		order_by="name asc",
		limit_page_length=1,
	)
	if not profile_row:
		return {}

	profile = profile_row[0]
	mode_of_payment = None
	if frappe.db.exists("DocType", "POS Payment Method"):
		payment_fields = ["mode_of_payment"]
		payment_fieldnames = _get_doctype_fieldnames("POS Payment Method")
		if "default" in payment_fieldnames:
			payment_fields.append("`default`")
		payments = frappe.get_all(
			"POS Payment Method",
			filters={"parent": profile.get("name"), "parenttype": "POS Profile"},
			fields=payment_fields,
			order_by="idx asc",
			page_length=0,
		)
		for row in payments:
			if row.get("default"):
				mode_of_payment = row.get("mode_of_payment")
				break
		if not mode_of_payment and payments:
			mode_of_payment = payments[0].get("mode_of_payment")

	return {
		"profile_name": profile.get("name"),
		"company": profile.get("company"),
		"warehouse": profile.get("warehouse"),
		"price_list": profile.get("selling_price_list"),
		"currency": profile.get("currency"),
		"mode_of_payment": mode_of_payment,
		"platform": platform_key,
	}


@frappe.whitelist(methods=["POST"], allow_guest=True)
@frappe.read_only()
@standard_api_response
def resolve_site(site_url: str | None = None, platform: str = "mobile") -> dict[str, Any]:
	settings = enforce_api_access(allow_guest=True)
	if not settings.allow_discovery:
		frappe.throw("Discovery endpoint is disabled")

	base_url = (site_url or get_url()).strip().rstrip("/")
	platform_key = (platform or "mobile").strip().lower()

	candidates = (
		["POS Desktop", "Desktop POS", "ERP-POS Clothing Center - Desktop"]
		if platform_key == "desktop"
		else ["Mobile POS", "ERP-POS Clothing Center", "POS Mobile"]
	)
	client = None
	for app_name in candidates:
		row = frappe.db.get_value(
			"OAuth Client",
			{"app_name": app_name},
			["client_id", "client_secret", "redirect_uris", "name"],
			as_dict=True,
		)
		if row:
			client = row
			break

	if not client:
		client = frappe.db.get_value(
			"OAuth Client",
			{},
			["client_id", "client_secret", "redirect_uris", "name"],
			order_by="creation asc",
			as_dict=True,
		)
	if not client:
		frappe.throw("OAuth Client is not configured")

	redirect_uri = ""
	redirect_uris = client.get("redirect_uris")
	if redirect_uris:
		redirect_uri = str(redirect_uris).split(",")[0].strip()

	data = {
		"url": base_url,
		"redirect_uri": redirect_uri,
		"clientId": client.get("client_id"),
		"clientSecret": client.get("client_secret") if settings.allow_client_secret_response else "",
		"scopes": ["all", "openid"],
		"name": client.get("name") or ("ERPNext POS Desktop" if platform_key == "desktop" else "ERPNext POS Mobile"),
		"lastUsedAt": None,
		"isFavorite": False,
		"api_version": settings.api_version,
		"runtime_defaults": _get_runtime_defaults(platform_key),
	}
	return ok(data)
