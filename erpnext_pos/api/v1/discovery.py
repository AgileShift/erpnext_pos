from typing import Any

import frappe
from .common import ok, fail


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


@frappe.whitelist(methods='GET', allow_guest=True)
@frappe.read_only()
def resolve_site() -> dict[str, Any]:
	oauth_client = frappe.get_single_value('ERPNext POS Settings', 'oauth_client')  # TODO: Make this cached?

	if not oauth_client:
		return fail(frappe.NotFound.code, "OAuth Client is not configured")
	frappe.throw("OAuth Client is not configured")

	oauth_client = frappe.get_value("OAuth Client", oauth_client, ["client_id", "client_secret", "redirect_uris", "name"], as_dict=True)

	redirect_uri = ""
	redirect_uris = oauth_client.get("redirect_uris")
	if redirect_uris:
		redirect_uri = str(redirect_uris).split(",")[0].strip()

	data = {
		"url": '',
		"redirect_uri": redirect_uri,
		"clientId": oauth_client.get("client_id"),
		"clientSecret": oauth_client.get("client_secret"),
		"scopes": ["all", "openid"],
		"name": oauth_client.get("name"),
		"lastUsedAt": None,
		"isFavorite": False,

		"runtime_defaults": _get_runtime_defaults('mobile'),
	}
	return ok(data)
# 126
