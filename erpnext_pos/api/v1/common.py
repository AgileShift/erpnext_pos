"""Utilidades transversales de API v1 (payload, idempotencia y respuesta estándar)."""

import hashlib
import json
from functools import wraps
from typing import Any

import frappe
from frappe.model.document import Document
from frappe.utils.data import add_to_date, now_datetime

IDEMPOTENCY_DOCTYPE = "ERPNext POS Idempotency Key"
_INTERNAL_FORM_KEYS = {
	"cmd",
	"data",
	"payload",
	"csrf_token",
	"_lang",
	"sid",
	"request_id",
	"client_request_id",
}


def _idempotency_available() -> bool:
	"""Return True only when idempotency storage doctype exists."""
	return bool(frappe.db.exists("DocType", IDEMPOTENCY_DOCTYPE))


def parse_payload(payload: str | dict[str, Any] | None) -> dict[str, Any]:
	"""Accept JSON string or dict payload and normalize to dict."""
	if isinstance(payload, (bytes, bytearray)):
		payload = payload.decode("utf-8")
	if payload is None:
		form = getattr(frappe.local, "form_dict", None) or {}
		raw_payload = form.get("payload")
		if raw_payload not in (None, ""):
			return parse_payload(raw_payload)
		fallback: dict[str, Any] = {}
		for key, value in form.items():
			if key in _INTERNAL_FORM_KEYS:
				continue
			if isinstance(value, str):
				text = value.strip()
				if text.startswith("{") or text.startswith("["):
					try:
						fallback[key] = json.loads(text)
						continue
					except Exception:
						pass
			fallback[key] = value
		return fallback
	if isinstance(payload, dict):
		return payload
	if isinstance(payload, str):
		text = payload.strip()
		if not text:
			return {}
		return json.loads(text)
	raise frappe.ValidationError(frappe._("Invalid payload type"))

# FIXME: DELETE
def value_from_aliases(body: dict[str, Any], *keys: str, default: Any = None) -> Any:
	for key in keys:
		if key not in body:
			continue
		value = body.get(key)
		if value is None:
			continue
		if isinstance(value, str) and not value.strip():
			continue
		return value
	return default


def to_bool(value: Any, *, default: bool = False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	if isinstance(value, str):
		return value.strip().lower() in {"1", "true", "yes", "y", "on"}
	return default


def ok(data: Any, request_id: str | None = None) -> dict[str, Any]:
	return {
		'success': True,
		'data': data,
		'error': None,

		# FIXME: If missing the data object is nested on itself
		'request_id': request_id,
		'server_time': now_datetime().isoformat(),
	}


def fail(code: str, message: str, details: Any = None, request_id: str | None = None) -> dict[str, Any]:
	return {
		"success": False,
		"data": None,
		"error": {"code": code, "message": message, "details": details},
		"request_id": request_id,
		"server_time": now_datetime().isoformat(),
	}


def _extract_request_id(kwargs: dict[str, Any]) -> str | None:
	last_payload: dict[str, Any] | None = None

	for key in ("client_request_id", "request_id"):
		value = kwargs.get(key)
		if isinstance(value, str) and value.strip():
			return value.strip()

	form = getattr(frappe.local, "form_dict", None) or {}
	for key in ("client_request_id", "request_id"):
		value = form.get(key)
		if isinstance(value, str) and value.strip():
			return value.strip()

	def _from_payload(value: Any) -> str | None:
		nonlocal last_payload
		try:
			body = parse_payload(value) if not isinstance(value, dict) else value
		except Exception:
			return None
		if isinstance(body, dict):
			last_payload = body
		for k in ("client_request_id", "clientRequestId", "request_id", "requestId"):
			v = body.get(k) if isinstance(body, dict) else None
			if isinstance(v, str) and v.strip():
				return v.strip()
		return None

	for source in (kwargs.get("payload"), form.get("payload")):
		if source in (None, ""):
			continue
		request_id = _from_payload(source)
		if request_id:
			return request_id
	if last_payload is not None:
		return payload_hash(last_payload)

	fallback: dict[str, Any] = {}
	for source in (kwargs, form):
		for key, value in (source or {}).items():
			if key in _INTERNAL_FORM_KEYS or key == "payload":
				continue
			if value is None:
				continue
			if isinstance(value, str) and not value.strip():
				continue
			fallback[key] = value
	if fallback:
		return payload_hash(fallback)
	return payload_hash({})


def _map_error_code(exc: Exception) -> str:
	if isinstance(exc, frappe.PermissionError):
		return "PERMISSION_DENIED"
	if isinstance(exc, frappe.AuthenticationError):
		return "AUTHENTICATION_ERROR"
	if isinstance(exc, frappe.ValidationError):
		return "VALIDATION_ERROR"
	if isinstance(exc, frappe.DoesNotExistError):
		return "NOT_FOUND"
	if isinstance(exc, frappe.LinkValidationError):
		return "LINK_VALIDATION_ERROR"
	return exc.__class__.__name__.upper()


def _is_expected_error(exc: Exception) -> bool:
	return isinstance(
		exc,
		(
			frappe.AuthenticationError,
			frappe.PermissionError,
			frappe.DoesNotExistError,
			frappe.ValidationError,
			frappe.LinkValidationError,
		),
	)


def standard_api_response(func):
	"""Ensure every API response uses the standard envelope for success and failure."""

	@wraps(func)
	def wrapper(*args, **kwargs):
		request_id = _extract_request_id(kwargs if isinstance(kwargs, dict) else {})
		try:
			result = func(*args, **kwargs)
			if isinstance(result, dict) and {"success", "error", "request_id", "server_time"}.issubset(result.keys()):
				if request_id and not result.get("request_id"):
					result["request_id"] = request_id
				return result
			return ok(result, request_id=request_id)
		except Exception as exc:
			# Avoid polluting logs for expected business/validation errors.
			if not _is_expected_error(exc):
				frappe.log_error(
					title=f"ERPNext POS API Error: {func.__name__}",
					message=frappe.get_traceback(),
				)

			return fail(
				code=_map_error_code(exc),
				message=str(exc) or "Unexpected error",
				details={"type": exc.__class__.__name__},
				request_id=request_id,
			)

	return wrapper


def payload_hash(payload: dict[str, Any]) -> str:
	raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _dget(doc: Document, key: str, default: Any = None) -> Any:
	"""Typed accessor for dynamic DocType fields."""
	return doc.get(key, default)


def _dset(doc: Document, key: str, value: Any) -> None:
	"""Typed setter for dynamic DocType fields."""
	doc.set(key, value)


def _save_idempotency_record(
	*,
	request_key: str,
	endpoint: str,
	request_hash_value: str,
	status: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	response_data: Any = None,
	error_message: str | None = None,
) -> None:
	if not _idempotency_available():
		return

	record = frappe.get_doc(
		{
			"doctype": IDEMPOTENCY_DOCTYPE,
			"request_key": request_key,
			"endpoint": endpoint,
			"request_hash": request_hash_value,
			"status": status,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"response_json": json.dumps(response_data, ensure_ascii=True) if response_data is not None else None,
			"error_message": error_message,
			"expires_on": add_to_date(now_datetime(), days=2, as_datetime=True),
		}
	)
	record.insert(ignore_permissions=True)


def get_idempotency_result(
	request_key: str,
	endpoint: str,
	request_hash_value: str,
) -> tuple[bool, Any]:
	"""Return (is_replay, data_or_none)."""
	if not _idempotency_available():
		return False, None

	existing_name = frappe.db.get_value(
		IDEMPOTENCY_DOCTYPE,
		{"request_key": request_key, "endpoint": endpoint},
		"name",
	)
	if not existing_name:
		return False, None

	existing = frappe.get_doc(IDEMPOTENCY_DOCTYPE, existing_name)
	existing_hash = _dget(existing, "request_hash")
	if existing_hash and existing_hash != request_hash_value:
		frappe.throw(
			frappe._("The same client_request_id was reused with different payload"),
			frappe.ValidationError,
		)
	status = _dget(existing, "status")
	if status == "Completed":
		response_json = _dget(existing, "response_json")
		data = json.loads(response_json) if response_json else None
		return True, data
	if status == "Failed":
		frappe.throw(_dget(existing, "error_message") or frappe._("Previous attempt failed"), frappe.ValidationError)
	return False, None


def complete_idempotency(
	request_key: str,
	endpoint: str,
	request_hash_value: str,
	response_data: Any,
	*,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> None:
	if not _idempotency_available():
		return

	existing_name = frappe.db.get_value(
		IDEMPOTENCY_DOCTYPE,
		{"request_key": request_key, "endpoint": endpoint},
		"name",
	)
	if existing_name:
		existing = frappe.get_doc(IDEMPOTENCY_DOCTYPE, existing_name)
		_dset(existing, "status", "Completed")
		_dset(existing, "request_hash", request_hash_value)
		_dset(existing, "reference_doctype", reference_doctype)
		_dset(existing, "reference_name", reference_name)
		_dset(existing, "response_json", json.dumps(response_data, ensure_ascii=True))
		_dset(existing, "error_message", None)
		existing.save(ignore_permissions=True)
		return

	_save_idempotency_record(
		request_key=request_key,
		endpoint=endpoint,
		request_hash_value=request_hash_value,
		status="Completed",
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		response_data=response_data,
	)


def fail_idempotency(
	request_key: str,
	endpoint: str,
	request_hash_value: str,
	error_message: str,
) -> None:
	if not _idempotency_available():
		return

	existing_name = frappe.db.get_value(
		IDEMPOTENCY_DOCTYPE,
		{"request_key": request_key, "endpoint": endpoint},
		"name",
	)
	if existing_name:
		existing = frappe.get_doc(IDEMPOTENCY_DOCTYPE, existing_name)
		_dset(existing, "status", "Failed")
		_dset(existing, "request_hash", request_hash_value)
		_dset(existing, "error_message", error_message)
		existing.save(ignore_permissions=True)
		return

	_save_idempotency_record(
		request_key=request_key,
		endpoint=endpoint,
		request_hash_value=request_hash_value,
		status="Failed",
		error_message=error_message,
	)


def require_client_request_id(client_request_id: str | None) -> str:
	request_id = (client_request_id or "").strip()
	if not request_id:
		frappe.throw(frappe._("client_request_id is required"), frappe.ValidationError)
	return request_id


def resolve_client_request_id(
	client_request_id: str | None,
	body: dict[str, Any] | None = None,
	*,
	required: bool = False,
) -> str:
	request_id = (client_request_id or "").strip()
	if request_id:
		return request_id

	for key in ("client_request_id", "clientRequestId", "request_id", "requestId"):
		value = (body or {}).get(key)
		if isinstance(value, str) and value.strip():
			return value.strip()

	if required:
		return require_client_request_id(client_request_id)

	# Deterministic fallback keeps idempotency without forcing client-side changes.
	user = str(getattr(getattr(frappe, "session", None), "user", "") or "Guest")
	return f"{user}:{payload_hash(body or {})}"
