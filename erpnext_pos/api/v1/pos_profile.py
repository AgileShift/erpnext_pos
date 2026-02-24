"""POS Profile endpoints for POS app integrations."""

from typing import Any

import frappe

from .common import ok, parse_payload, standard_api_response, value_from_aliases
from .settings import enforce_api_access
from .sync import _get_pos_profile_detail


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def detail(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	"""Return POS Profile details (including payment methods) without requiring an open shift."""
	enforce_api_access()
	body = parse_payload(payload)
	profile_name = str(
		value_from_aliases(body, "profile_name", "profileName", "pos_profile", "posProfile",
						   default="") or ""
	).strip()
	if not profile_name:
		frappe.throw("Missing POS Profile")

	profile_detail = _get_pos_profile_detail(profile_name)
	if not profile_detail:
		frappe.throw(f"POS Profile {profile_name} not found")
	return ok(profile_detail)
