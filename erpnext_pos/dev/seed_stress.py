from __future__ import annotations

"""Seed data for stress testing the POS mobile app."""

from typing import Any

import random
import frappe
from frappe.utils import nowdate, now_datetime, add_days


def seed_stress_data(
	customers: int = 1000,
	items: int = 1500,
	invoices: int = 3000,
	*,
	seed_prefix: str = "STRESS",
	paid_ratio: float = 0.6,
	partial_ratio: float = 0.25,
	always_create_customers: bool = True,
	company: str | None = None,
	warehouse: str | None = None,
	price_list: str | None = None,
	pos_profile: str | None = None,
	commit_every: int = 200,
) -> dict[str, Any]:
	"""Create customers, items, stock, and invoices for stress testing."""

	random.seed(20260214)
	company = company or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw("No Company found")

	company_currency = frappe.db.get_value("Company", company, "default_currency")
	default_receivable = frappe.db.get_value("Company", company, "default_receivable_account")

	def _first_account(filters: dict[str, Any]) -> str | None:
		return frappe.db.get_value("Account", filters, "name")

	income_account = (
		_first_account({"company": company, "account_type": "Income", "is_group": 0})
		or _first_account({"company": company, "account_type": "Sales", "is_group": 0})
		or _first_account({"company": company, "root_type": "Income", "is_group": 0})
		or _first_account({"company": company, "report_type": "Profit and Loss", "is_group": 0})
	)
	if not income_account:
		frappe.throw(f"No Income/Sales account found for company {company}")

	if not default_receivable:
		default_receivable = _first_account({"company": company, "account_type": "Receivable", "is_group": 0})

	warehouse = warehouse or frappe.db.get_value(
		"Warehouse",
		{"is_group": 0},
		"name",
	)
	if not warehouse:
		frappe.throw("No Warehouse found")

	price_list = price_list or frappe.db.get_value("Price List", {"selling": 1}, "name")
	if not price_list:
		frappe.throw("No selling Price List found")

	pos_profile = pos_profile or frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
	if not pos_profile:
		frappe.throw("No POS Profile found")

	mode_of_payment = frappe.db.get_value(
		"POS Payment Method",
		{"parent": pos_profile, "parenttype": "POS Profile"},
		"mode_of_payment",
	)
	mop_account = None
	if mode_of_payment:
		mop_account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mode_of_payment, "company": company},
			"default_account",
		)

	customer_group = (
		frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		or frappe.db.get_value("Customer Group", {}, "name")
		or "All Customer Groups"
	)
	territory = (
		frappe.db.get_value("Territory", {"is_group": 0}, "name")
		or frappe.db.get_value("Territory", {}, "name")
		or "All Territories"
	)
	item_group = (
		frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		or frappe.db.get_value("Item Group", {}, "name")
		or "All Item Groups"
	)
	stock_uom = frappe.db.get_value("UOM", {}, "name") or "Unit"

	created_customers = 0
	for idx in range(1, customers + 1):
		name = f"{seed_prefix}-CUST-{idx:05d}"
		if frappe.db.exists("Customer", name):
			if not always_create_customers:
				continue
			name = f"{seed_prefix}-CUST-{idx:05d}-{frappe.generate_hash(length=6)}"
		doc = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_group": customer_group,
				"territory": territory,
				"customer_type": "Individual",
			}
		)
		doc.insert(ignore_permissions=True)
		created_customers += 1
		if created_customers % commit_every == 0:
			frappe.db.commit()

	created_items = 0
	item_codes: list[str] = []
	for idx in range(1, items + 1):
		code = f"{seed_prefix}-ITEM-{idx:05d}"
		item_codes.append(code)
		if frappe.db.exists("Item", code):
			continue
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": item_group,
				"stock_uom": stock_uom,
				"is_stock_item": 1,
				"is_sales_item": 1,
				"is_purchase_item": 0,
			}
		)
		item.append(
			"item_defaults",
			{
				"company": company,
				"default_warehouse": warehouse,
				"income_account": income_account,
			},
		)
		item.append(
			"reorder_levels",
			{
				"warehouse": warehouse,
				"warehouse_reorder_level": 10,
				"warehouse_reorder_qty": 20,
			},
		)
		try:
			item.insert(ignore_permissions=True)
		except Exception as exc:
			frappe.throw(f"Failed to create Item {code}: {exc}")
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": code,
				"price_list": price_list,
				"price_list_rate": random.randint(10, 200),
				"currency": company_currency,
			}
		).insert(ignore_permissions=True)
		created_items += 1
		if created_items % commit_every == 0:
			frappe.db.commit()

	if not item_codes:
		item_codes = [name for name in frappe.get_all("Item", pluck="name", page_length=0) if name]
		if not item_codes:
			frappe.throw("No items available to create invoices")

	# Seed stock via material receipt in batches.
	if items:
		batch_size = 100
		for start in range(0, len(item_codes), batch_size):
			batch = item_codes[start : start + batch_size]
			entries = []
			for i, code in enumerate(batch):
				# Create some items with low/zero stock to trigger alerts.
				if (start + i) % 10 == 0:
					qty = 1
				elif (start + i) % 10 == 1:
					qty = 3
				else:
					qty = random.randint(20, 100)
				entries.append(
					{
						"item_code": code,
						"qty": qty,
						"basic_rate": random.randint(10, 200),
						"t_warehouse": warehouse,
					}
				)
			stock_entry = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Receipt",
					"company": company,
					"posting_date": nowdate(),
					"posting_time": now_datetime().time(),
					"items": entries,
				}
			)
			stock_entry.insert(ignore_permissions=True)
			stock_entry.submit()
			frappe.db.commit()

	# Create invoices
	all_customers = frappe.get_all(
		"Customer",
		filters={"name": ["like", f"{seed_prefix}-CUST-%"]},
		pluck="name",
		page_length=0,
	)
	if not all_customers:
		all_customers = [row.get("name") for row in frappe.get_all("Customer", pluck="name", page_length=0)]
	if not all_customers:
		frappe.throw("No customers available to create invoices")

	payment_modes = []
	if mode_of_payment:
		payment_modes.append({"mode_of_payment": mode_of_payment, "account": mop_account})

	created_invoices = 0
	for idx in range(1, invoices + 1):
		customer = random.choice(all_customers)
		item_count = random.randint(1, 3)
		items_rows = []
		for _ in range(item_count):
			code = random.choice(item_codes)
			qty = random.randint(1, 5)
			rate = random.randint(10, 200)
			items_rows.append(
				{
					"item_code": code,
					"qty": qty,
					"rate": rate,
					"income_account": income_account,
				}
			)

		r = random.random()
		is_paid = r <= paid_ratio
		is_partial = (r > paid_ratio) and (r <= paid_ratio + partial_ratio)

		inv = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": customer,
				"company": company,
				"posting_date": nowdate(),
				"due_date": add_days(nowdate(), 30),
				"currency": company_currency,
				"conversion_rate": 1,
				"selling_price_list": price_list,
				"debit_to": default_receivable,
				"is_pos": 0,
				"pos_profile": None,
				"set_posting_time": 1,
				"update_stock": 0,
				"items": items_rows,
			}
		)
		inv.insert(ignore_permissions=True)
		target_amount = 0.0
		if is_paid or is_partial:
			target_amount = float(inv.grand_total or 0)
			if is_partial:
				target_amount = max(target_amount * 0.5, 1)

		inv.submit()

		if (is_paid or is_partial) and mode_of_payment:
			pe = frappe.get_doc(
				{
					"doctype": "Payment Entry",
					"payment_type": "Receive",
					"party_type": "Customer",
					"party": customer,
					"company": company,
					"posting_date": nowdate(),
					"mode_of_payment": mode_of_payment,
					"paid_from": default_receivable,
					"paid_to": mop_account,
					"paid_amount": target_amount,
					"received_amount": target_amount,
					"reference_no": f"{seed_prefix}-PE-{idx:06d}",
					"reference_date": nowdate(),
				}
			)
			pe.append(
				"references",
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": inv.name,
					"total_amount": inv.grand_total,
					"outstanding_amount": inv.outstanding_amount,
					"allocated_amount": target_amount,
				},
			)
			pe.insert(ignore_permissions=True)
			pe.submit()
		created_invoices += 1
		if created_invoices % commit_every == 0:
			frappe.db.commit()

	frappe.db.commit()
	return {
		"company": company,
		"warehouse": warehouse,
		"price_list": price_list,
		"pos_profile": pos_profile,
		"created_customers": created_customers,
		"created_items": created_items,
		"created_invoices": created_invoices,
	}


def seed_categories_and_payment_terms(
	*,
	seed_prefix: str = "STRESS",
	categories: int = 12,
	payment_terms: int = 10,
	item_sample: int = 2000,
) -> dict[str, Any]:
	"""Create Item Groups and Payment Terms for stress tests."""
	random.seed(20260214)

	root_item_group = frappe.db.get_value("Item Group", {"is_group": 1}, "name") or "All Item Groups"
	if not frappe.db.exists("Item Group", root_item_group):
		frappe.throw("No root Item Group found")

	created_groups = 0
	group_names: list[str] = []
	for idx in range(1, categories + 1):
		name = f"{seed_prefix}-CAT-{idx:02d}"
		if not frappe.db.exists("Item Group", name):
			doc = frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": name,
					"parent_item_group": root_item_group,
					"is_group": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			created_groups += 1
		group_names.append(name)

	# Assign categories to items.
	items = frappe.get_all("Item", pluck="name", page_length=0)
	if items:
		random.shuffle(items)
		limit = min(item_sample, len(items))
		for idx, item_code in enumerate(items[:limit]):
			group = group_names[idx % len(group_names)]
			frappe.db.set_value("Item", item_code, "item_group", group, update_modified=False)

	# Payment Terms
	created_terms = 0
	for idx in range(1, payment_terms + 1):
		name = f"{seed_prefix}-TERM-{idx:02d}"
		if frappe.db.exists("Payment Term", name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Payment Term",
				"payment_term_name": name,
				"invoice_portion": 100,
				"credit_days": 15 * idx,
			}
		)
		doc.insert(ignore_permissions=True)
		created_terms += 1

	frappe.db.commit()
	return {
		"created_item_groups": created_groups,
		"total_item_groups": len(group_names),
		"items_updated": min(item_sample, len(items)),
		"created_payment_terms": created_terms,
	}
