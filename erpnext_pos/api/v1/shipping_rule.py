import frappe


def get_shipping_rules():
	# TODO: Assuming its a Fixed
	return frappe.get_all('Shipping Rule', fields=['label', 'shipping_amount'])
