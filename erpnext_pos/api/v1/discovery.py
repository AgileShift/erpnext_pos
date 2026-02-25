from typing import Any

import frappe
from .common import ok


@frappe.whitelist(methods='GET', allow_guest=True)
@frappe.read_only()
def resolve_site(platform: str) -> dict[str, Any]:

	if platform not in ('desktop', 'mobile'):
		frappe.throw(f'Invalid platform: {platform}.')

	settings = frappe.get_single('ERPNext POS Settings')  # FIXME frappe.get_cached_doc

	oauth_client = {
		'desktop': settings.desktop_oauth_client,
		'mobile': settings.mobile_oauth_client,
	}[platform]

	if not oauth_client:
		frappe.throw(f'OAuth Client for {platform} is not configured.')

	oauth_client = frappe.get_value(
		'OAuth Client', oauth_client, [
			'name', 'client_id', 'default_redirect_uri', 'scopes', 'redirect_uris'
		], as_dict=True)

	if not oauth_client:
		frappe.throw(f'OAuth Client {oauth_client} does not exist.')

	data = {
		'name': oauth_client.get('name'),
		'client_id': oauth_client.get('client_id'),
		'default_redirect_uri': oauth_client.get('default_redirect_uri'),
		'scopes': oauth_client.get('scopes').split(' '),
		'redirect_uris': oauth_client.get('redirect_uris').split('\n')
	}

	return ok(data)
