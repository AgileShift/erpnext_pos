from __future__ import annotations

"""Endpoints de Transferencia Interna usando Payment Entry (Internal Transfer)."""

from typing import Any

import frappe
from frappe.utils.data import nowdate

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


_INTERNAL_MUTATION_KEYS = {"client_request_id", "clientRequestId", "request_id", "requestId", "payload", "cmd"}


def _coerce_float(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except Exception:
		return default


def _normalize_create_payload(body: dict[str, Any]) -> dict[str, Any]:
	doc_payload = {k: v for k, v in body.items() if k not in _INTERNAL_MUTATION_KEYS}
	alias_map = {
		"company": value_from_aliases(body, "company"),
		"posting_date": value_from_aliases(body, "posting_date", "postingDate", default=nowdate()),
		"payment_type": "Internal Transfer",
		"party_type": value_from_aliases(body, "party_type", "partyType"),
		"party": value_from_aliases(body, "party", "party_id", "partyId", "customer", "customerId"),
		"mode_of_payment": value_from_aliases(body, "mode_of_payment", "modeOfPayment"),
		"paid_amount": value_from_aliases(body, "paid_amount", "paidAmount"),
		"received_amount": value_from_aliases(body, "received_amount", "receivedAmount"),
		"paid_from": value_from_aliases(body, "paid_from", "paidFrom"),
		"paid_to": value_from_aliases(body, "paid_to", "paidTo"),
		"paid_to_account_currency": value_from_aliases(body, "paid_to_account_currency", "paidToAccountCurrency"),
		"source_exchange_rate": value_from_aliases(body, "source_exchange_rate", "sourceExchangeRate"),
		"target_exchange_rate": value_from_aliases(body, "target_exchange_rate", "targetExchangeRate"),
		"reference_no": value_from_aliases(body, "reference_no", "referenceNo"),
		"reference_date": value_from_aliases(body, "reference_date", "referenceDate"),
	}
	for key, value in alias_map.items():
		if value is None:
			continue
		doc_payload[key] = value

	doc_payload["paid_amount"] = _coerce_float(doc_payload.get("paid_amount"), 0.0)
	doc_payload["received_amount"] = _coerce_float(doc_payload.get("received_amount"), 0.0)
	if not str(doc_payload.get("party") or "").strip():
		doc_payload.pop("party", None)
	if not str(doc_payload.get("party_type") or "").strip():
		doc_payload.pop("party_type", None)
	doc_payload.pop("doctype", None)
	doc_payload.pop("docstatus", None)
	return doc_payload


def _validate_create_payload(doc_payload: dict[str, Any]) -> None:
	company = str(doc_payload.get("company") or "").strip()
	paid_from = str(doc_payload.get("paid_from") or "").strip()
	paid_to = str(doc_payload.get("paid_to") or "").strip()
	paid_amount = _coerce_float(doc_payload.get("paid_amount"), 0.0)
	received_amount = _coerce_float(doc_payload.get("received_amount"), 0.0)

	if not company:
		frappe.throw("company is required")
	if not paid_from:
		frappe.throw("paid_from is required")
	if not paid_to:
		frappe.throw("paid_to is required")
	if paid_from and paid_to and paid_from == paid_to:
		frappe.throw("paid_from and paid_to must be different")
	if paid_amount <= 0 and received_amount <= 0:
		frappe.throw("paid_amount or received_amount must be greater than 0")


@frappe.whitelist(methods=["POST"])
@standard_api_response
def create_submit(payload: str | dict[str, Any] | None = None, client_request_id: str | None = None) -> dict[str, Any]:
	enforce_api_access()
	enforce_doctype_permission("Payment Entry", "create")
	enforce_doctype_permission("Payment Entry", "submit")
	body = parse_payload(payload)
	request_id = resolve_client_request_id(
		client_request_id or str(value_from_aliases(body, "client_request_id", "clientRequestId", default="") or ""),
		body,
	)
	endpoint = "internal_transfer.create_submit"
	request_hash_value = payload_hash(body)
	replay, replay_data = get_idempotency_result(request_id, endpoint, request_hash_value)
	if replay:
		return ok(replay_data, request_id=request_id)

	doc_payload = _normalize_create_payload(body)
	_validate_create_payload(doc_payload)
	doc_payload["doctype"] = "Payment Entry"
	doc = frappe.get_doc(doc_payload)
	doc.insert(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()
	result = {
		"name": doc.name,
		"docstatus": int(doc.docstatus or 0),
		"payment_type": doc.get("payment_type"),
		"party_type": doc.get("party_type"),
		"party": doc.get("party"),
		"paid_from": doc.get("paid_from"),
		"paid_to": doc.get("paid_to"),
		"paid_amount": _coerce_float(doc.get("paid_amount"), 0.0),
		"received_amount": _coerce_float(doc.get("received_amount"), 0.0),
		"posting_date": str(doc.get("posting_date")) if doc.get("posting_date") else None,
		"modified": str(doc.get("modified")) if doc.get("modified") else None,
	}

	complete_idempotency(
		request_id,
		endpoint,
		request_hash_value,
		result,
		reference_doctype="Payment Entry",
		reference_name=doc.name,
	)
	return ok(result, request_id=request_id)
