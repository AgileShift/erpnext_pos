from __future__ import annotations

"""Endpoints de sesión POS: apertura y cierre atómicos de turno/caja."""

import re
from typing import Any

import frappe
from frappe.utils.data import now_datetime, nowdate

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
from .sync import _get_accessible_pos_profiles


PLACEHOLDER_PATTERN = re.compile(r"^\s*\{\{.+\}\}\s*$")


def _clean_scalar(value: Any) -> Any:
	if not isinstance(value, str):
		return value
	text = value.strip()
	if not text:
		return None
	if PLACEHOLDER_PATTERN.match(text):
		return None
	return text


def _get_doctype_fieldnames(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	return set(frappe.get_all("DocField", filters={"parent": doctype}, pluck="fieldname", page_length=0))


def _get_default_user_profile_name(user: str, allowed_profile_names: set[str]) -> str | None:
	if not frappe.db.exists("DocType", "POS Profile User"):
		return None
	pfu_fields = _get_doctype_fieldnames("POS Profile User")
	if "user" not in pfu_fields or "parent" not in pfu_fields:
		return None

	fields = ["parent"]
	if "default" in pfu_fields:
		fields.append("`default`")
	filters: dict[str, Any] = {"user": user}
	if "parenttype" in pfu_fields:
		filters["parenttype"] = "POS Profile"

	rows = frappe.get_all("POS Profile User", filters=filters, fields=fields, order_by="idx asc", page_length=0)
	for row in rows:
		parent = row.get("parent")
		if row.get("default") and parent in allowed_profile_names:
			return parent
	for row in rows:
		parent = row.get("parent")
		if parent in allowed_profile_names:
			return parent
	return None


def _resolve_profile_for_opening(requested_profile: Any) -> str:
	profiles = _get_accessible_pos_profiles(frappe.session.user)
	allowed_names = {row.get("name") for row in profiles if row.get("name")}
	if not allowed_names:
		frappe.throw("User does not have accessible POS Profile")

	requested = _clean_scalar(requested_profile)
	if requested:
		if requested not in allowed_names:
			frappe.throw(f"User {frappe.session.user} does not have access to POS Profile {requested}.")
		return str(requested)

	default_profile = _get_default_user_profile_name(frappe.session.user, allowed_names)
	if default_profile:
		return default_profile
	return next(iter(sorted(allowed_names)))


def _normalize_balance_details(profile_name: str, body: dict[str, Any]) -> list[dict[str, Any]]:
	body_balance = value_from_aliases(body, "balance_details", "balanceDetails")
	if isinstance(body_balance, list):
		output: list[dict[str, Any]] = []
		for row in body_balance:
			if not isinstance(row, dict):
				continue
			mode = _clean_scalar(value_from_aliases(row, "mode_of_payment", "modeOfPayment"))
			if not mode:
				continue
			opening_amount = value_from_aliases(row, "opening_amount", "openingAmount")
			try:
				opening_amount = float(opening_amount or 0)
			except Exception:
				opening_amount = 0.0
			output.append({"mode_of_payment": mode, "opening_amount": opening_amount})
		if output:
			return output

	mode_from_body = _clean_scalar(value_from_aliases(body, "mode_of_payment", "modeOfPayment"))
	opening_amount_value = value_from_aliases(body, "opening_amount", "openingAmount")
	try:
		opening_amount = float(opening_amount_value or 0)
	except Exception:
		opening_amount = 0.0
	if mode_from_body:
		return [{"mode_of_payment": mode_from_body, "opening_amount": opening_amount}]

	payment_rows = frappe.get_all(
		"POS Payment Method",
		filters={"parent": profile_name, "parenttype": "POS Profile"},
		fields=["mode_of_payment"],
		order_by="idx asc",
		page_length=0,
	)
	modes = [row.get("mode_of_payment") for row in payment_rows if row.get("mode_of_payment")]
	if not modes:
		frappe.throw(
			"No mode_of_payment found for POS Profile. Provide payload.mode_of_payment or configure POS Profile payments."
		)
	return [{"mode_of_payment": mode, "opening_amount": opening_amount} for mode in modes]


def _build_opening_payload(body: dict[str, Any]) -> dict[str, Any]:
	doc_fields = _get_doctype_fieldnames("POS Opening Entry")
	doc_payload = {k: v for k, v in body.items() if k in doc_fields and k != "balance_details"}
	pos_profile = _resolve_profile_for_opening(
		value_from_aliases(body, "pos_profile", "posProfile", "profile_name", "profileName")
	)
	company = _clean_scalar(value_from_aliases(body, "company")) or frappe.db.get_value("POS Profile", pos_profile, "company")
	session_user = frappe.session.user
	payload_user = _clean_scalar(value_from_aliases(body, "user"))
	if payload_user and payload_user != session_user:
		frappe.throw("payload.user must match authenticated user")
	user = session_user
	period_start = _clean_scalar(value_from_aliases(body, "period_start_date", "periodStartDate")) or now_datetime()
	posting_date = _clean_scalar(value_from_aliases(body, "posting_date", "postingDate")) or nowdate()

	if not company:
		frappe.throw(f"Company could not be resolved for POS Profile {pos_profile}")

	doc_payload["pos_profile"] = pos_profile
	doc_payload["company"] = company
	doc_payload["user"] = user
	doc_payload["period_start_date"] = period_start
	doc_payload["posting_date"] = posting_date
	doc_payload["balance_details"] = _normalize_balance_details(pos_profile, body)
	return doc_payload


def _find_existing_open_opening(*, pos_profile: str | None, user: str | None) -> dict[str, Any] | None:
	base_filters: dict[str, Any] = {"docstatus": 1, "status": "Open"}
	fields = ["name", "status", "pos_profile", "user", "company", "posting_date", "period_start_date"]
	if not user:
		return None

	filters = {**base_filters, "user": user}
	if pos_profile:
		filters["pos_profile"] = pos_profile

	rows = frappe.get_all(
		"POS Opening Entry",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit_page_length=1,
	)
	return rows[0] if rows else None


@frappe.whitelist(methods=["POST"])
@standard_api_response
def opening_create_submit(
	payload: str | dict[str, Any] | None = None,
	client_request_id: str | None = None,
) -> dict[str, Any]:
	enforce_api_access()
	enforce_doctype_permission("POS Opening Entry", "create")
	enforce_doctype_permission("POS Opening Entry", "submit")
	body = parse_payload(payload)
	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "pos_opening.create_submit"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc_payload = _build_opening_payload(body)
	existing_open = _find_existing_open_opening(
		pos_profile=doc_payload.get("pos_profile"),
		user=doc_payload.get("user"),
	)
	if existing_open:
		result = {"name": existing_open.get("name"), "reused": True, "status": existing_open.get("status") or "Open"}
		complete_idempotency(
			request_id,
			endpoint,
			request_hash_value,
			result,
			reference_doctype="POS Opening Entry",
			reference_name=existing_open.get("name"),
		)
		return ok(result, request_id=request_id)

	doc_payload["doctype"] = "POS Opening Entry"
	doc = frappe.get_doc(doc_payload)
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()
	result = {"name": doc.name}

	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype="POS Opening Entry",
		reference_name=doc.name,
	)
	return ok(result, request_id=request_id)


@frappe.whitelist(methods=["POST"])
@standard_api_response
def closing_create_submit(
	payload: str | dict[str, Any] | None = None,
	client_request_id: str | None = None,
) -> dict[str, Any]:
	enforce_api_access()
	enforce_doctype_permission("POS Closing Entry", "create")
	enforce_doctype_permission("POS Closing Entry", "submit")
	body = parse_payload(payload)
	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "pos_closing.create_submit"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc_payload = dict(body)
	doc_payload["doctype"] = "POS Closing Entry"
	doc = frappe.get_doc(doc_payload)
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()
	result = {"name": doc.name}

	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype="POS Closing Entry",
		reference_name=doc.name,
	)
	return ok(result, request_id=request_id)
