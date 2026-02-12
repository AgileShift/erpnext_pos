from __future__ import annotations

from frappe.model.document import Document

from erpnext_pos.access import apply_settings_access_controls


class ERPNextPOSSettings(Document):
	def on_update(self):
		if self.flags.get("skip_access_apply"):
			return
		apply_settings_access_controls(self)
