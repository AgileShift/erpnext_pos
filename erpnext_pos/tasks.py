from __future__ import annotations

import frappe
from frappe.utils.data import now_datetime


def cleanup_idempotency_keys():
	if not frappe.db.exists("DocType", "ERPNext POS Idempotency Key"):
		return
	frappe.db.delete(
		"ERPNext POS Idempotency Key",
		{"expires_on": ["<", now_datetime()]},
	)
