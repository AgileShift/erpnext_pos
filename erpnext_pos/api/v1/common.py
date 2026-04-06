"""Utilidades transversales de API v1 y respuesta estándar."""

from functools import wraps
from typing import Any

import frappe
from frappe.utils.data import now_datetime


def parse_payload(payload: str | dict[str, Any] | None) -> dict[str, Any]:
	if payload is None:
		return {}
	if isinstance(payload, dict):
		return dict(payload)
	parsed = frappe.parse_json(payload)
	if isinstance(parsed, dict):
		return dict(parsed)
	frappe.throw("payload must be a JSON object")


def ok(data: Any) -> dict[str, Any]:
	return {
		'success': True,
		'data': data,
		'error': None,
		'server_time': now_datetime().isoformat(),
	}


def fail(code: str, message: str, details: Any = None) -> dict[str, Any]:
	return {
		"success": False,
		"data": None,
		"error": {"code": code, "message": message, "details": details},
		"server_time": now_datetime().isoformat(),
	}


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
		try:
			result = func(*args, **kwargs)
			if isinstance(result, dict) and {"success", "error", "server_time"}.issubset(result.keys()):
				return result
			return ok(result)
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
			)

	return wrapper
