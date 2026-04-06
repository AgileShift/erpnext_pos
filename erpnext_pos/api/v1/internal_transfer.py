from __future__ import annotations

"""Endpoints de Transferencia Interna usando Payment Entry (Internal Transfer)."""

from typing import Any

import frappe
from frappe.utils.data import nowdate

from .common import (
	ok,
	parse_payload,
	standard_api_response,
)


_INTERNAL_MUTATION_KEYS = {"client_request_id", "request_id", "payload", "cmd"}


def _coerce_float(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except Exception:
		return default
def _normalize_create_payload(body: dict[str, Any]) -> dict[str, Any]:
	doc_payload = {k: v for k, v in body.items() if k not in _INTERNAL_MUTATION_KEYS}
	for fieldname in (
		"company",
		"posting_date",
		"party_type",
		"party",
		"mode_of_payment",
		"paid_amount",
		"received_amount",
		"paid_from",
		"paid_to",
		"paid_to_account_currency",
		"source_exchange_rate",
		"target_exchange_rate",
		"reference_no",
		"reference_date",
	):
		value = body.get(fieldname)
		if value is None:
			continue
		doc_payload[fieldname] = value

	doc_payload["payment_type"] = "Internal Transfer"
	doc_payload.setdefault("posting_date", nowdate())
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
def create_submit(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	body = parse_payload(payload)

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

	return ok(result)
