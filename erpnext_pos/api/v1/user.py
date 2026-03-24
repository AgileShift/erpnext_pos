from typing import Any

import frappe

from .common import ok, standard_api_response


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def get_authenticated_user() -> dict[str, Any]:
	user = (frappe.session.user or "Guest").strip() or "Guest"
	if user == "Guest":
		frappe.throw("Login required")

	data = frappe.db.get_values('User', filters={"name": user}, fieldname=[
		"email",
		"first_name",
		"last_name",
		"username",
		"user_image",
		"language",
		"time_zone",
		"full_name",
	], as_dict=True, for_update=False, limit=1)[0]

	data['user'] = user

	return ok(data)
