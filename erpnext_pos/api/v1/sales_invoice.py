from __future__ import annotations

from typing import Any

import frappe

from .common import (
	complete_idempotency,
	get_idempotency_result,
	ok,
	parse_payload,
	payload_hash,
	resolve_client_request_id,
	standard_api_response,
	value_from_aliases,
)
from .settings import enforce_api_access, enforce_doctype_permission


@frappe.whitelist(methods=["POST"])
@standard_api_response
def create_submit(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
	enforce_api_access()
	enforce_doctype_permission("Sales Invoice", "create")
	enforce_doctype_permission("Sales Invoice", "submit")
	body = parse_payload(payload)
	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "sales_invoice.create_submit"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc_payload = dict(body)
	doc_payload["doctype"] = "Sales Invoice"
	doc = frappe.get_doc(doc_payload)
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()
	result = doc.as_dict()

	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype="Sales Invoice",
		reference_name=doc.name,
	)
	return ok(result, request_id=request_id)


@frappe.whitelist(methods=["POST"])
@standard_api_response
def cancel(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	name = (body.get("name") or "").strip()
	if not name:
		frappe.throw("name is required")

	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "sales_invoice.cancel"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc = frappe.get_doc("Sales Invoice", name)
	enforce_doctype_permission("Sales Invoice", "cancel", doc=doc)
	doc.flags.ignore_permissions = True
	doc.cancel()
	result = {"name": doc.name, "docstatus": doc.docstatus}

	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype="Sales Invoice",
		reference_name=doc.name,
	)
	return ok(result, request_id=request_id)
