from __future__ import annotations

import frappe
from frappe.model.document import Document

from erpnext_pos.access import apply_settings_access_controls


class ERPNextPOSSettings(Document):
	"""Configuración central del backend API para POS móvil."""

	def validate(self):
		self._sync_legacy_allowed_roles_csv()
		self._normalize_inventory_alert_thresholds()
		self._normalize_inventory_alert_rules()

	def on_update(self):
		if self.flags.get("skip_access_apply"):
			return
		apply_settings_access_controls(self)

	def _sync_legacy_allowed_roles_csv(self) -> None:
		"""Mantiene el campo legacy en sync para compatibilidad con instalaciones previas."""
		rows = self.get("allowed_api_roles_table") or []
		roles: list[str] = []
		seen: set[str] = set()
		for row in rows:
			role = str(row.get("role") or "").strip()
			if not role or role in seen:
				continue
			seen.add(role)
			roles.append(role)
		self.allowed_api_roles = ",".join(roles)

	def _normalize_inventory_alert_thresholds(self) -> None:
		critical = self._to_float(self.get("inventory_alert_critical_ratio"), 0.35)
		low = self._to_float(self.get("inventory_alert_low_ratio"), 1.0)
		if critical < 0:
			critical = 0
		if low < critical:
			low = critical
		self.inventory_alert_critical_ratio = critical
		self.inventory_alert_low_ratio = low

	@staticmethod
	def _to_float(value, default: float) -> float:
		try:
			return float(value)
		except Exception:
			return default

	@staticmethod
	def _to_int(value, default: int) -> int:
		try:
			return int(value)
		except Exception:
			return default

	def _normalize_inventory_alert_rules(self) -> None:
		"""Normaliza reglas por bodega/grupo para cálculo consistente de alertas."""
		rows = self.get("inventory_alert_rules") or []
		default_critical = self._to_float(self.get("inventory_alert_critical_ratio"), 0.35)
		default_low = self._to_float(self.get("inventory_alert_low_ratio"), 1.0)
		if default_critical < 0:
			default_critical = 0.0
		if default_low < default_critical:
			default_low = default_critical

		seen: set[tuple[str, str]] = set()
		for row in rows:
			row.enabled = 1 if row.get("enabled") else 0
			warehouse = str(row.get("warehouse") or "").strip()
			item_group = str(row.get("item_group") or "").strip()
			key = (warehouse or "*", item_group or "*")
			if key in seen:
				frappe.throw(
					frappe._('Duplicate inventory alert rule for Warehouse "{0}" and Item Group "{1}".').format(
						warehouse or "*",
						item_group or "*",
					)
				)
			seen.add(key)

			critical = self._to_float(row.get("critical_ratio"), default_critical)
			low = self._to_float(row.get("low_ratio"), default_low)
			if critical < 0:
				critical = 0.0
			if low < critical:
				low = critical
			priority = self._to_int(row.get("priority"), 10)
			if priority < 0:
				priority = 0

			row.critical_ratio = critical
			row.low_ratio = low
			row.priority = priority
