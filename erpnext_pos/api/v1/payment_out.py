from __future__ import annotations

"""Endpoints de Pago (Pay) usando Payment Entry."""

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


def _normalize_references(value: Any) -> list[dict[str, Any]]:
	rows = value if isinstance(value, list) else []
	references: list[dict[str, Any]] = []
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		row = dict(raw)
		reference_doctype = str(
			value_from_aliases(row, "reference_doctype", "referenceDoctype", default="Purchase Invoice") or ""
		).strip()
		reference_name = str(value_from_aliases(row, "reference_name", "referenceName", default="") or "").strip()
		if not reference_name:
			continue
		row["reference_doctype"] = reference_doctype or "Purchase Invoice"
		row["reference_name"] = reference_name
		if "allocated_amount" in row or "allocatedAmount" in row:
			row["allocated_amount"] = _coerce_float(value_from_aliases(row, "allocated_amount", "allocatedAmount"), 0.0)
		if "outstanding_amount" in row or "outstandingAmount" in row:
			row["outstanding_amount"] = _coerce_float(
				value_from_aliases(row, "outstanding_amount", "outstandingAmount"), 0.0
			)
		if "total_amount" in row or "totalAmount" in row:
			row["total_amount"] = _coerce_float(value_from_aliases(row, "total_amount", "totalAmount"), 0.0)
		references.append(row)
	return references


def _normalize_create_payload(body: dict[str, Any]) -> dict[str, Any]:
	doc_payload = {k: v for k, v in body.items() if k not in _INTERNAL_MUTATION_KEYS}
	alias_map = {
		"company": value_from_aliases(body, "company"),
		"posting_date": value_from_aliases(body, "posting_date", "postingDate", default=nowdate()),
		"payment_type": "Pay",
		"party_type": value_from_aliases(body, "party_type", "partyType", default="Supplier"),
		"party": value_from_aliases(body, "party", "party_id", "partyId", "supplier", "supplierId"),
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
	doc_payload["references"] = _normalize_references(value_from_aliases(body, "references", default=[]))
	if not str(doc_payload.get("paid_to") or "").strip():
		company = str(doc_payload.get("company") or "").strip()
		party = str(doc_payload.get("party") or "").strip()
		payable_account = None
		if company and party and frappe.db.exists("DocType", "Supplier Account"):
			payable_account = frappe.db.get_value(
				"Supplier Account",
				{"parent": party, "company": company},
				"account",
			)
		if not payable_account and company:
			payable_account = frappe.db.get_value("Company", company, "default_payable_account")
		if payable_account:
			doc_payload["paid_to"] = payable_account
	doc_payload.pop("doctype", None)
	doc_payload.pop("docstatus", None)
	return doc_payload


def _validate_create_payload(doc_payload: dict[str, Any]) -> None:
	company = str(doc_payload.get("company") or "").strip()
	party = str(doc_payload.get("party") or "").strip()
	party_type = str(doc_payload.get("party_type") or "").strip()
	paid_amount = _coerce_float(doc_payload.get("paid_amount"), 0.0)
	received_amount = _coerce_float(doc_payload.get("received_amount"), 0.0)
	references = doc_payload.get("references") if isinstance(doc_payload.get("references"), list) else []

	if not company:
		frappe.throw("company is required")
	if not party:
		frappe.throw("party is required")
	if not party_type:
		frappe.throw("party_type is required")
	if paid_amount <= 0 and received_amount <= 0:
		frappe.throw("paid_amount or received_amount must be greater than 0")

	for idx, ref in enumerate(references, start=1):
		if not str(ref.get("reference_name") or "").strip():
			frappe.throw(f"references[{idx}].reference_name is required")
		if not str(ref.get("reference_doctype") or "").strip():
			frappe.throw(f"references[{idx}].reference_doctype is required")
		allocated_amount = _coerce_float(ref.get("allocated_amount"), 0.0)
		if allocated_amount <= 0:
			frappe.throw(f"references[{idx}].allocated_amount must be greater than 0")


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
	endpoint = "payment_out.create_submit"
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
