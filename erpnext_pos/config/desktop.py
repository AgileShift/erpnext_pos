from __future__ import annotations

import frappe


def get_data():
	return [
		{
			"module_name": "ERPNext POS",
			"type": "module",
			"label": frappe._("ERPNext POS"),
		}
	]
