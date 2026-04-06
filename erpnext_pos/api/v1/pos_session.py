from typing import Any

import frappe
from frappe.utils import flt
from frappe.utils.data import now_datetime, nowdate

from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
	make_closing_entry_from_opening,
)

from .common import ok, parse_payload, standard_api_response


def _get_user_profile_rows(user: str) -> list[dict[str, Any]]:
	profile_rows = frappe.get_all(
		"POS Profile User",
		filters={"user": user, "parenttype": "POS Profile"},
		fields=["parent", "default"],
		order_by="idx asc",
		page_length=0,
	)
	if not profile_rows:
		frappe.throw(f"User {user} does not have an assigned POS Profile")
	return profile_rows


def _resolve_pos_profile_name(user: str, body: dict[str, Any]) -> str:
	requested_profile = str(body.get("pos_profile") or body.get("profile_name") or "").strip()
	profile_rows = _get_user_profile_rows(user)
	allowed_profiles = {row["parent"] for row in profile_rows if row.get("parent")}

	if requested_profile:
		if requested_profile not in allowed_profiles:
			frappe.throw(f"User {user} does not have access to POS Profile {requested_profile}")
		return requested_profile

	for row in profile_rows:
		if row.get("default") and row.get("parent") in allowed_profiles:
			return row["parent"]

	return next(row["parent"] for row in profile_rows if row.get("parent") in allowed_profiles)


def _get_profile_payment_methods(profile_name: str) -> list[dict[str, Any]]:
	return frappe.get_all(
		"POS Payment Method",
		filters={"parent": profile_name, "parenttype": "POS Profile"},
		fields=["mode_of_payment"],
		order_by="idx asc",
		page_length=0,
	)


def _build_balance_details(profile_name: str, body: dict[str, Any]) -> list[dict[str, Any]]:
	balance_rows = body.get("balance_details")
	if isinstance(balance_rows, list):
		normalized_rows = []
		for row in balance_rows:
			if not isinstance(row, dict):
				continue
			mode_of_payment = str(row.get("mode_of_payment") or "").strip()
			if not mode_of_payment:
				continue
			normalized_rows.append(
				{
					"mode_of_payment": mode_of_payment,
					"opening_amount": flt(row.get("opening_amount")),
				}
			)
		if normalized_rows:
			return normalized_rows

	mode_of_payment = str(body.get("mode_of_payment") or "").strip()
	opening_amount = flt(body.get("opening_amount"))
	if mode_of_payment:
		return [{"mode_of_payment": mode_of_payment, "opening_amount": opening_amount}]

	payment_methods = _get_profile_payment_methods(profile_name)
	if not payment_methods:
		frappe.throw(f"POS Profile {profile_name} does not have configured payment methods")

	return [
		{
			"mode_of_payment": row["mode_of_payment"],
			"opening_amount": opening_amount,
		}
		for row in payment_methods
		if row.get("mode_of_payment")
	]


def _get_existing_opening(user: str, profile_name: str) -> dict[str, Any] | None:
	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"user": user, "pos_profile": profile_name, "status": "Open", "docstatus": 1},
		fields=["name", "status", "period_start_date", "posting_date"],
		order_by="modified desc",
		page_length=1,
	)
	return rows[0] if rows else None


def _get_open_shift(profile_name: str | None, opening_name: str | None) -> dict[str, Any] | None:
	filters: dict[str, Any] = {"status": "Open", "docstatus": 1}
	if opening_name:
		filters["name"] = str(opening_name).strip()
	if profile_name:
		filters["pos_profile"] = str(profile_name).strip()
	if frappe.session.user and frappe.session.user != "Guest":
		filters["user"] = frappe.session.user

	rows = frappe.get_all(
		"POS Opening Entry",
		filters=filters,
		fields=[
			"name",
			"status",
			"pos_profile",
			"company",
			"user",
			"period_start_date",
			"period_end_date",
			"posting_date",
			"pos_closing_entry",
			"modified",
		],
		order_by="modified desc",
		page_length=1,
	)
	return rows[0] if rows else None


def _normalize_closing_amounts(body: dict[str, Any]) -> dict[str, float]:
	closing_amounts: dict[str, float] = {}
	rows = body.get("payment_reconciliation") or []
	if not isinstance(rows, list):
		return closing_amounts

	for row in rows:
		if isinstance(row, dict):
			mode_of_payment = str(row.get("mode_of_payment") or "").strip()
			if mode_of_payment:
				closing_amounts[mode_of_payment] = flt(row.get("closing_amount"))
			continue

		if isinstance(row, (list, tuple)) and len(row) >= 2:
			mode_of_payment = str(row[0] or "").strip()
			if mode_of_payment:
				closing_amounts[mode_of_payment] = flt(row[1])

	return closing_amounts


def _apply_closing_amounts(doc, closing_amounts: dict[str, float]) -> None:
	payment_rows = {row.mode_of_payment: row for row in doc.get("payment_reconciliation") or [] if row.mode_of_payment}

	for mode_of_payment, closing_amount in closing_amounts.items():
		row = payment_rows.get(mode_of_payment)
		if row:
			row.closing_amount = closing_amount
			continue

		row = doc.append(
			"payment_reconciliation",
			{
				"mode_of_payment": mode_of_payment,
				"opening_amount": 0,
				"expected_amount": 0,
				"closing_amount": closing_amount,
			},
		)
		payment_rows[mode_of_payment] = row

	for row in doc.get("payment_reconciliation") or []:
		if row.closing_amount in (None, ""):
			row.closing_amount = row.expected_amount
		row.difference = flt(row.closing_amount) - flt(row.expected_amount)


@frappe.whitelist(methods=["POST"])
@standard_api_response
def opening_create_submit(payload: str | dict[str, Any] | None) -> dict[str, Any]:
	body = parse_payload(payload)
	session_user = frappe.session.user
	payload_user = str(body.get("user") or "").strip()
	if payload_user and payload_user != session_user:
		frappe.throw("payload.user must match authenticated user")

	profile_name = _resolve_pos_profile_name(session_user, body)
	existing_opening = _get_existing_opening(session_user, profile_name)
	if existing_opening:
		return ok(
			{
				"name": existing_opening["name"],
				"reused": True,
				"status": existing_opening.get("status") or "Open",
			}
		)

	company = str(body.get("company") or "").strip() or frappe.db.get_value("POS Profile", profile_name, "company")
	if not company:
		frappe.throw(f"Company could not be resolved for POS Profile {profile_name}")

	doc = frappe.get_doc(
		{
			"doctype": "POS Opening Entry",
			"pos_profile": profile_name,
			"company": company,
			"user": session_user,
			"period_start_date": body.get("period_start_date") or now_datetime(),
			"posting_date": body.get("posting_date") or nowdate(),
			"balance_details": _build_balance_details(profile_name, body),
		}
	)
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()

	return ok({"name": doc.name, "reused": False, "status": doc.status})


@frappe.whitelist(methods=["POST"])
@standard_api_response
def closing_create_submit(payload: str | dict[str, Any] | None) -> dict[str, Any]:
	body = parse_payload(payload)
	pos_opening_entry = str(body.get("pos_opening_entry") or "").strip()
	if not pos_opening_entry:
		frappe.throw("pos_opening_entry is required")

	opening_entry = frappe.get_doc("POS Opening Entry", pos_opening_entry)
	payload_user = str(body.get("user") or "").strip()
	if payload_user and payload_user != opening_entry.user:
		frappe.throw("payload.user must match the POS Opening Entry user")

	doc = make_closing_entry_from_opening(opening_entry)
	doc.period_end_date = body.get("period_end_date") or doc.period_end_date
	doc.posting_date = body.get("posting_date") or doc.posting_date
	doc.posting_time = body.get("posting_time") or doc.posting_time
	_apply_closing_amounts(doc, _normalize_closing_amounts(body))
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()

	return ok({"name": doc.name, "status": doc.status})


@frappe.whitelist(methods=["POST", "GET"])
@standard_api_response
def closing_for_opening(payload: str | dict[str, Any] | None) -> dict[str, Any]:
	body = parse_payload(payload)
	pos_opening_entry = str(body.get("pos_opening_entry") or "").strip()
	if not pos_opening_entry:
		frappe.throw("pos_opening_entry is required")

	opening_entry = frappe.get_doc("POS Opening Entry", pos_opening_entry)
	doc = make_closing_entry_from_opening(opening_entry)
	doc.period_end_date = body.get("period_end_date") or doc.period_end_date
	doc.posting_date = body.get("posting_date") or doc.posting_date
	doc.posting_time = body.get("posting_time") or doc.posting_time
	_apply_closing_amounts(doc, _normalize_closing_amounts(body))

	return ok(doc.as_dict())
