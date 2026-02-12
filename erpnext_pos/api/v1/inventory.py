from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import quote

import frappe
from frappe.utils.data import get_url

from .common import ok, parse_payload, standard_api_response, value_from_aliases
from .settings import enforce_api_access, get_settings


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
@standard_api_response
def list_with_alerts(payload: str | dict[str, Any] | None = None) -> dict[str, Any]:
	enforce_api_access()
	body = parse_payload(payload)
	warehouse = str(value_from_aliases(body, "warehouse", "warehouse_id", "warehouseId", default="") or "").strip()
	price_list = str(value_from_aliases(body, "price_list", "priceList", default="") or "").strip()
	offset = int(value_from_aliases(body, "offset", default=0) or 0)
	limit = int(
		value_from_aliases(
			body,
			"limit",
			"page_size",
			"pageSize",
			default=(get_settings().default_sync_page_size or 50),
		)
		or 0
	)

	if not warehouse:
		frappe.throw("warehouse is required")

	items = _build_inventory_items(warehouse=warehouse, price_list=price_list, offset=offset, limit=limit)
	alerts = _build_inventory_alerts(warehouse=warehouse, items=items)
	items = _apply_inventory_visibility_rules(items=items, alerts=alerts)

	return ok({"items": items, "alerts": alerts})


def _get_doctype_fieldnames(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	return set(frappe.get_all("DocField", filters={"parent": doctype}, pluck="fieldname", page_length=0))


def _get_item_barcodes(item_codes: list[str]) -> dict[str, str]:
	if not item_codes or not frappe.db.exists("DocType", "Item Barcode"):
		return {}
	filters: dict[str, Any] = {"parent": ["in", item_codes]}
	if "parenttype" in _get_doctype_fieldnames("Item Barcode"):
		filters["parenttype"] = "Item"
	rows = frappe.get_all(
		"Item Barcode",
		filters=filters,
		fields=["parent", "barcode"],
		order_by="idx asc",
		page_length=0,
	)
	barcode_by_item: dict[str, str] = {}
	for row in rows:
		parent = row.get("parent")
		barcode = (row.get("barcode") or "").strip()
		if parent and barcode and parent not in barcode_by_item:
			barcode_by_item[parent] = barcode
	return barcode_by_item


def _get_item_variant_descriptors(item_codes: list[str]) -> dict[str, str]:
	if not item_codes or not frappe.db.exists("DocType", "Item Variant Attribute"):
		return {}
	filters: dict[str, Any] = {"parent": ["in", item_codes]}
	if "parenttype" in _get_doctype_fieldnames("Item Variant Attribute"):
		filters["parenttype"] = "Item"
	rows = frappe.get_all(
		"Item Variant Attribute",
		filters=filters,
		fields=["parent", "attribute", "attribute_value"],
		order_by="idx asc",
		page_length=0,
	)
	descriptor_map: dict[str, list[str]] = {}
	for row in rows:
		parent = row.get("parent")
		if not parent:
			continue
		attribute = (row.get("attribute") or "").strip()
		value = (row.get("attribute_value") or "").strip()
		if not value:
			continue
		text = f"{attribute}: {value}" if attribute else value
		descriptor_map.setdefault(parent, []).append(text)
	return {item_code: ", ".join(values) for item_code, values in descriptor_map.items() if values}


def _build_inventory_items(warehouse: str, price_list: str, offset: int, limit: int) -> list[dict[str, Any]]:
	sales_items = frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_sales_item": 1},
		fields=["name", "item_code"],
		order_by="item_code asc",
		page_length=0,
	)
	merged_codes = sorted(
		{
			str((row.get("item_code") or row.get("name") or "")).strip()
			for row in sales_items
			if str((row.get("item_code") or row.get("name") or "")).strip()
		}
	)
	start = max(int(offset or 0), 0)
	page_length = max(int(limit or 0), 0)
	if page_length > 0:
		item_codes = merged_codes[start : start + page_length]
	else:
		item_codes = merged_codes[start:]
	if not item_codes:
		return []

	bins = frappe.get_all(
		"Bin",
		filters={"warehouse": warehouse, "item_code": ["in", item_codes]},
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "projected_qty", "stock_uom", "valuation_rate"],
		order_by="item_code asc",
		page_length=0,
	)
	bin_by_code = {row.get("item_code"): row for row in bins if row.get("item_code")}

	items = frappe.get_all(
		"Item",
		filters={"name": ["in", item_codes]},
		fields=[
			"item_code",
			"item_name",
			"item_group",
			"description",
			"brand",
			"image",
			"stock_uom",
			"standard_rate",
			"is_stock_item",
			"is_sales_item",
			"variant_of",
			"disabled",
		],
		page_length=0,
	)
	item_by_code = {row.get("item_code"): row for row in items if row.get("item_code")}
	barcode_by_code = _get_item_barcodes(item_codes)
	variant_descriptors = _get_item_variant_descriptors(item_codes)

	price_filters = {"item_code": ["in", item_codes], "selling": 1}
	if price_list:
		price_filters["price_list"] = price_list
	prices = frappe.get_all(
		"Item Price",
		filters=price_filters,
		fields=["item_code", "price_list", "price_list_rate", "currency"],
		page_length=0,
		order_by="modified desc",
	)
	price_by_code: dict[str, dict[str, Any]] = {}
	for row in prices:
		item_code = row.get("item_code")
		if item_code:
			price_by_code.setdefault(item_code, row)

	output: list[dict[str, Any]] = []
	for item_code in item_codes:
		bin_row = bin_by_code.get(item_code) or {}
		item = item_by_code.get(item_code)
		if not item:
			continue
		item_code_text = str(item_code or "").strip()
		item_path_segment = quote(item_code_text, safe="")
		desk_route = f"/desk/item/{item_path_segment}"
		desk_url = f"{get_url()}{desk_route}#details"
		price_row = price_by_code.get(item_code)
		actual_qty = float(bin_row.get("actual_qty") or 0)
		reserved_qty = float(bin_row.get("reserved_qty") or 0)
		sellable_qty = max(actual_qty - reserved_qty, 0)
		price = (price_row.get("price_list_rate") if price_row else item.get("standard_rate")) or 0
		currency = price_row.get("currency") if price_row else ""
		is_stocked = bool(item.get("is_stock_item"))
		is_service = (not is_stocked) or (item.get("item_group") == "COMPLEMENTARIOS")
		variant_description = (variant_descriptors.get(item_code) or "").strip()
		item_name = item.get("item_name") or item_code
		if variant_description and variant_description.lower() not in str(item_name).lower():
			item_name = f"{item_name} ({variant_description})"
		output.append(
			{
				"item_code": item_code,
				"actual_qty": sellable_qty,
				"_raw_actual_qty": actual_qty,
				"price": price,
				"valuation_rate": bin_row.get("valuation_rate") or 0,
				"name": item_name,
				"item_group": item.get("item_group") or "",
				"description": item.get("description") or "",
				"barcode": barcode_by_code.get(item_code, ""),
				"image": item.get("image") or "",
				"discount": 0.0,
				"is_service": 1 if is_service else 0,
				"is_stocked": 1 if is_stocked else 0,
				"stock_uom": item.get("stock_uom") or bin_row.get("stock_uom") or "",
				"brand": item.get("brand") or "",
				"currency": currency or "",
				"projected_qty": bin_row.get("projected_qty") or actual_qty,
				"variant_of": item.get("variant_of") or None,
				"variant_attributes": variant_description or None,
				"desk_route": desk_route,
				"desk_url": desk_url,
			}
		)
	return output


def _build_inventory_alerts(warehouse: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
	settings = get_settings()
	if not settings.enable_inventory_alerts:
		return []

	item_codes = [row["item_code"] for row in items if row.get("item_code")]
	if not item_codes:
		return []

	reorders = frappe.get_all(
		"Item Reorder",
		filters={"warehouse": warehouse, "parent": ["in", item_codes]},
		fields=["parent as item_code", "warehouse_reorder_level", "warehouse_reorder_qty"],
		page_length=0,
	)
	reorder_by_item = {row.get("item_code"): row for row in reorders if row.get("item_code")}
	alert_limit = int(settings.inventory_alert_default_limit or 20)
	critical_ratio_default = float(settings.inventory_alert_critical_ratio or 0.35)

	rules = frappe.get_all(
		"ERPNext POS Inventory Alert Rule",
		filters={"enabled": 1},
		fields=["warehouse", "item_group", "critical_ratio", "low_ratio", "priority"],
		page_length=0,
		order_by="priority asc",
	)
	rules_by_warehouse: dict[str, list[Any]] = defaultdict(list)
	for rule in rules:
		key = rule.warehouse or "*"
		rules_by_warehouse[key].append(rule)

	alerts: list[dict[str, Any]] = []
	for row in items:
		if not row.get("is_stocked"):
			continue
		item_code = row["item_code"]
		projected_qty = float(row.get("projected_qty") or row.get("actual_qty") or 0)
		reorder_row = reorder_by_item.get(item_code)
		reorder_level = (
			float(reorder_row.get("warehouse_reorder_level"))
			if reorder_row and reorder_row.get("warehouse_reorder_level") is not None
			else None
		)
		reorder_qty = (
			float(reorder_row.get("warehouse_reorder_qty"))
			if reorder_row and reorder_row.get("warehouse_reorder_qty") is not None
			else None
		)

		critical_ratio = critical_ratio_default
		low_ratio = 1.0
		for rule in rules_by_warehouse.get(warehouse, []) + rules_by_warehouse.get("*", []):
			if rule.get("item_group") and rule.get("item_group") != row.get("item_group"):
				continue
			critical_ratio = float(rule.get("critical_ratio") or critical_ratio_default)
			low_ratio = float(rule.get("low_ratio") or 1.0)
			break

		status = None
		if projected_qty <= 0:
			status = "CRITICAL"
		elif reorder_level and reorder_level > 0:
			if projected_qty <= reorder_level * critical_ratio:
				status = "CRITICAL"
			elif projected_qty <= reorder_level * low_ratio:
				status = "LOW"

		if status:
			alerts.append(
				{
					"itemCode": item_code,
					"item_code": item_code,
					"itemName": row.get("name") or item_code,
					"item_name": row.get("name") or item_code,
					"qty": projected_qty,
					"status": status,
					"reorderLevel": reorder_level,
					"reorder_level": reorder_level,
					"reorderQty": reorder_qty,
					"reorder_qty": reorder_qty,
				}
			)

	alerts.sort(key=lambda it: (it["status"] != "CRITICAL", it["qty"]))
	return alerts[:alert_limit]


def _apply_inventory_visibility_rules(items: list[dict[str, Any]], alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Hide negative stock rows unless they have a stock alert."""
	alert_codes = {
		str((row.get("itemCode") or row.get("item_code") or "")).strip()
		for row in alerts
		if (row.get("itemCode") or row.get("item_code"))
	}
	output: list[dict[str, Any]] = []
	for row in items:
		item_code = str(row.get("item_code") or "").strip()
		raw_qty_value = row.get("_raw_actual_qty")
		if raw_qty_value is None:
			raw_qty_value = row.get("actual_qty")
		try:
			raw_qty = float(raw_qty_value or 0)
		except (TypeError, ValueError):
			raw_qty = 0.0
		if raw_qty < 0 and item_code not in alert_codes:
			continue
		clean_row = dict(row)
		clean_row.pop("_raw_actual_qty", None)
		output.append(clean_row)
	return output
