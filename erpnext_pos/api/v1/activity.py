from __future__ import annotations

"""Registro y consulta de actividad entre cajeros para notificaciones en app."""

import json
from typing import Any

import frappe
from frappe.model.document import Document
from frappe.utils.data import now_datetime

from .common import ok, parse_payload, standard_api_response, to_bool, value_from_aliases
from .settings import enforce_api_access


ACTIVITY_PREFIX = "[ERPNext POS]"
DEFAULT_ACTIVITY_LIMIT = 50
MAX_ACTIVITY_LIMIT = 200


def _as_int(value: Any, default: int) -> int:
	try:
		return int(value)
	except Exception:
		return default


def _safe_full_name(user: str) -> str:
	normalized = (user or "").strip() or "Guest"
	if normalized == "Guest":
		return "Guest"
	return frappe.db.get_value("User", normalized, "full_name") or normalized


def _build_subject(event_type: str, action: str, reference_name: str | None) -> str:
	base = f"{ACTIVITY_PREFIX} {event_type} {action}".strip()
	if reference_name:
		base = f"{base}: {reference_name}"
	return base[:140]


def _to_json_text(value: Any) -> str:
	try:
		return json.dumps(value, ensure_ascii=True, default=str)
	except Exception:
		return "{}"


def record_cashier_activity(
	*,
	event_type: str,
	action: str,
	reference_doctype: str,
	reference_name: str,
	message: str | None = None,
	company: str | None = None,
	pos_profile: str | None = None,
	warehouse: str | None = None,
	territory: str | None = None,
	route: str | None = None,
	payload: dict[str, Any] | None = None,
	actor: str | None = None,
) -> None:
	"""Persist cashier activity in Frappe Activity Log.

	Best effort by design: activity logging should never break business transactions.
	"""
	try:
		if not frappe.db.exists("DocType", "Activity Log"):
			return

		actor_user = (actor or frappe.session.user or "Guest").strip() or "Guest"
		full_name = _safe_full_name(actor_user)
		reference_name_value = (reference_name or "").strip()
		if not reference_name_value:
			return

		activity_payload = {
			"app": "erpnext_pos",
			"event_type": (event_type or "").strip(),
			"action": (action or "").strip(),
			"message": (message or "").strip() or None,
			"reference_doctype": (reference_doctype or "").strip(),
			"reference_name": reference_name_value,
			"actor": actor_user,
			"actor_full_name": full_name,
			"company": (company or "").strip() or None,
			"pos_profile": (pos_profile or "").strip() or None,
			"warehouse": (warehouse or "").strip() or None,
			"territory": (territory or "").strip() or None,
			"route": (route or "").strip() or None,
			"timestamp": now_datetime().isoformat(),
			"payload": payload or {},
		}

		doc = frappe.get_doc(
			{
				"doctype": "Activity Log",
				"subject": _build_subject(event_type, action, reference_name_value),
				"content": _to_json_text(activity_payload),
				"communication_date": now_datetime(),
				"reference_doctype": (reference_doctype or "").strip() or None,
				"reference_name": reference_name_value,
				"timeline_doctype": (reference_doctype or "").strip() or None,
				"timeline_name": reference_name_value,
				"link_doctype": (reference_doctype or "").strip() or None,
				"link_name": reference_name_value,
				"user": actor_user,
				"full_name": full_name,
			}
		)
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title="ERPNext POS Activity Log Error",
			message=frappe.get_traceback(),
		)


def _parse_content(raw_content: Any) -> dict[str, Any]:
	text = (raw_content or "").strip() if isinstance(raw_content, str) else ""
	if not text:
		return {}
	try:
		value = json.loads(text)
		return value if isinstance(value, dict) else {}
	except Exception:
		return {}


def _normalize_event_row(row: dict[str, Any], current_user: str) -> dict[str, Any]:
	content = _parse_content(row.get("content"))
	event_type = (content.get("event_type") or row.get("reference_doctype") or "").strip()
	action = (content.get("action") or "").strip()
	actor = (row.get("user") or content.get("actor") or "").strip()
	actor_full_name = (row.get("full_name") or content.get("actor_full_name") or actor).strip()
	created_on = row.get("communication_date") or row.get("creation")

	return {
		"name": row.get("name"),
		"event_type": event_type,
		"action": action or None,
		"title": row.get("subject"),
		"message": content.get("message") or row.get("subject"),
		"actor": actor,
		"actor_full_name": actor_full_name,
		"is_other_cashier": 1 if actor and actor != current_user else 0,
		"reference_doctype": row.get("reference_doctype") or content.get("reference_doctype"),
		"reference_name": row.get("reference_name") or content.get("reference_name"),
		"company": content.get("company"),
		"pos_profile": content.get("pos_profile"),
		"warehouse": content.get("warehouse"),
		"territory": content.get("territory"),
		"route": content.get("route"),
		"created_on": created_on,
		"payload": content.get("payload") or {},
	}


def _normalize_filter_value(value: Any) -> str | None:
	text = str(value or "").strip()
	return text.lower() if text else None


def _event_matches_context(
	event: dict[str, Any],
	*,
	company: str | None,
	pos_profile: str | None,
	warehouse: str | None,
	territory: str | None,
	route: str | None,
) -> bool:
	expected = {
		"company": _normalize_filter_value(company),
		"pos_profile": _normalize_filter_value(pos_profile),
		"warehouse": _normalize_filter_value(warehouse),
		"territory": _normalize_filter_value(territory),
		"route": _normalize_filter_value(route),
	}
	for key, expected_value in expected.items():
		if not expected_value:
			continue
		current_value = _normalize_filter_value(event.get(key))
		if current_value and current_value != expected_value:
			return False
	return True


def get_cashier_activity_events(
	*,
	modified_since: str | None,
	limit: int = DEFAULT_ACTIVITY_LIMIT,
	offset: int = 0,
	only_other_cashiers: bool = True,
	event_types: list[str] | None = None,
	company: str | None = None,
	pos_profile: str | None = None,
	warehouse: str | None = None,
	territory: str | None = None,
	route: str | None = None,
) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "Activity Log"):
		return []

	limit_value = max(1, min(_as_int(limit, DEFAULT_ACTIVITY_LIMIT), MAX_ACTIVITY_LIMIT))
	offset_value = max(0, _as_int(offset, 0))
	current_user = (frappe.session.user or "Guest").strip() or "Guest"
	allowed_event_types = {str(value or "").strip().lower() for value in (event_types or []) if str(value or "").strip()}

	filters: dict[str, Any] = {"subject": ["like", f"{ACTIVITY_PREFIX}%"]}
	if modified_since:
		filters["modified"] = [">=", modified_since]
	if only_other_cashiers and current_user != "Guest":
		filters["user"] = ["!=", current_user]

	base_fields = [
		"name",
		"subject",
		"content",
		"communication_date",
		"reference_doctype",
		"reference_name",
		"user",
		"full_name",
		"creation",
		"modified",
	]
	chunk_size = min(MAX_ACTIVITY_LIMIT, max(limit_value * 2, 50))
	max_scan = MAX_ACTIVITY_LIMIT * 10
	scanned = 0
	start = offset_value
	result: list[dict[str, Any]] = []

	while len(result) < limit_value and scanned < max_scan:
		rows = frappe.get_all(
			"Activity Log",
			filters=filters,
			fields=base_fields,
			order_by="communication_date desc, creation desc",
			page_length=chunk_size,
			start=start,
		)
		if not rows:
			break

		start += len(rows)
		scanned += len(rows)
		for row in rows:
			event = _normalize_event_row(row, current_user)
			if allowed_event_types and str(event.get("event_type") or "").strip().lower() not in allowed_event_types:
				continue
			if not _event_matches_context(
				event,
				company=company,
				pos_profile=pos_profile,
				warehouse=warehouse,
				territory=territory,
				route=route,
			):
				continue
			result.append(event)
			if len(result) >= limit_value:
				break

	return result


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def pull(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	modified_since = str(value_from_aliases(body, "modified_since", "modifiedSince", default="") or "").strip() or None
	limit = _as_int(value_from_aliases(body, "limit", "page_size", "pageSize", default=DEFAULT_ACTIVITY_LIMIT), DEFAULT_ACTIVITY_LIMIT)
	offset = _as_int(value_from_aliases(body, "offset", default=0), 0)
	company = str(value_from_aliases(body, "company", default="") or "").strip() or None
	pos_profile = str(
		value_from_aliases(body, "pos_profile", "posProfile", "profile_name", "profileName", default="") or ""
	).strip() or None
	warehouse = str(value_from_aliases(body, "warehouse", "warehouse_id", "warehouseId", default="") or "").strip() or None
	territory = str(value_from_aliases(body, "territory", default="") or "").strip() or None
	route = str(value_from_aliases(body, "route", default="") or "").strip() or None
	only_other_cashiers = to_bool(
		value_from_aliases(body, "only_other_cashiers", "onlyOtherCashiers", default=True),
		default=True,
	)
	raw_event_types = value_from_aliases(body, "event_types", "eventTypes", default=[]) or []
	event_types: list[str] = []
	if isinstance(raw_event_types, list):
		event_types = [str(value or "").strip() for value in raw_event_types if str(value or "").strip()]

	events = get_cashier_activity_events(
		modified_since=modified_since,
		limit=limit,
		offset=offset,
		only_other_cashiers=only_other_cashiers,
		event_types=event_types,
		company=company,
		pos_profile=pos_profile,
		warehouse=warehouse,
		territory=territory,
		route=route,
	)
	return ok(
		{
			"events": events,
			"count": len(events),
			"only_other_cashiers": only_other_cashiers,
		}
	)


def on_customer_after_insert(doc: Document, method: str | None = None) -> None:
	record_cashier_activity(
		event_type="Customer",
		action="Created",
		reference_doctype=doc.doctype,
		reference_name=doc.name,
		message=f"Customer {doc.name} created",
		territory=doc.get("territory"),
		route=doc.get("route"),
		payload={"method": method},
	)


def on_sales_invoice_on_submit(doc: Document, method: str | None = None) -> None:
	record_cashier_activity(
		event_type="Sales Invoice",
		action="Submitted",
		reference_doctype=doc.doctype,
		reference_name=doc.name,
		message=f"Sales Invoice {doc.name} submitted",
		company=doc.get("company"),
		pos_profile=doc.get("pos_profile"),
		territory=doc.get("territory"),
		route=doc.get("route"),
		payload={"grand_total": doc.get("grand_total"), "customer": doc.get("customer"), "method": method},
	)


def on_sales_invoice_on_cancel(doc: Document, method: str | None = None) -> None:
	record_cashier_activity(
		event_type="Sales Invoice",
		action="Cancelled",
		reference_doctype=doc.doctype,
		reference_name=doc.name,
		message=f"Sales Invoice {doc.name} cancelled",
		company=doc.get("company"),
		pos_profile=doc.get("pos_profile"),
		territory=doc.get("territory"),
		route=doc.get("route"),
		payload={"grand_total": doc.get("grand_total"), "customer": doc.get("customer"), "method": method},
	)


def on_payment_entry_on_submit(doc: Document, method: str | None = None) -> None:
	record_cashier_activity(
		event_type="Payment Entry",
		action="Submitted",
		reference_doctype=doc.doctype,
		reference_name=doc.name,
		message=f"Payment Entry {doc.name} submitted",
		company=doc.get("company"),
		territory=doc.get("territory"),
		route=doc.get("route"),
		payload={
			"party": doc.get("party"),
			"party_type": doc.get("party_type"),
			"paid_amount": doc.get("paid_amount"),
			"received_amount": doc.get("received_amount"),
			"method": method,
		},
	)
