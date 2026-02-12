from __future__ import annotations

import frappe


def get_data():
	return [
		{
			"label": frappe._("POS Mobile Configuration"),
			"icon": "fa fa-mobile",
			"items": [
				{
					"type": "doctype",
					"name": "ERPNext POS Settings",
					"description": frappe._("Single form to configure API, discovery and inventory alerts."),
				}
			],
		}
	]
