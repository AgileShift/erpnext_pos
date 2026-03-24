from pypika.queries import QueryBuilder

import frappe


# FIXME: WE NEED TO ADD THE COMPANY FILTER -> ASAP!
def _get_pos_payment_methods_query(pos_profiles: list[str]):
	"""Return the POS Payment Methods for a POS Profile."""
	pos_payment_method = frappe.qb.DocType('POS Payment Method')
	mode_of_payment_account = frappe.qb.DocType('Mode of Payment Account')
	account = frappe.qb.DocType('Account')

	# FIXME: Add Mode of Payment Type: Bank, or Cash
	return (
		frappe.qb.from_(pos_payment_method)
		.inner_join(mode_of_payment_account).on(pos_payment_method.mode_of_payment == mode_of_payment_account.parent)
		.inner_join(account).on(mode_of_payment_account.default_account == account.name)
		.where(pos_payment_method.parent.isin(pos_profiles))
		.select(
			pos_payment_method.name,
			pos_payment_method.parent,
			pos_payment_method.default,
			pos_payment_method.allow_in_returns,
			pos_payment_method.mode_of_payment,
			mode_of_payment_account.default_account,
			account.account_currency
		)
		.orderby(pos_payment_method.idx)
	)


def _get_user_pos_profiles_query(user: str = frappe.session.user) -> QueryBuilder:
	""" Return the POS Profiles for the actual user. """
	pos_profile = frappe.qb.DocType('POS Profile')
	pos_profile_user = frappe.qb.DocType('POS Profile User')

	# TODO: Add Company Filter
	return (
		frappe.qb.from_(pos_profile)
		.inner_join(pos_profile_user).on(pos_profile.name == pos_profile_user.parent)
		.where(pos_profile_user.user == user)
		.where(pos_profile.disabled == False)
		.select(
			pos_profile.name,
			pos_profile.company,
			pos_profile.warehouse,

			# Configuration
			pos_profile.ignore_pricing_rule,
			pos_profile.allow_rate_change,
			pos_profile.allow_discount_change,
			pos_profile.set_grand_total_to_default_mop,
			pos_profile.allow_partial_payment,

			# Accounting
			pos_profile.selling_price_list,
			pos_profile.currency,
			pos_profile.apply_discount_on,
			pos_profile.disable_rounded_total,

			# Accounting Dimensions
			pos_profile.cost_center
		)
		.orderby(pos_profile.idx)
	)


@frappe.whitelist(allow_guest=False, methods=['GET'])
def user_pos_profiles() -> list:
	"""Return the POS Profiles name for the actual user."""

	if not (pos_profiles := _get_user_pos_profiles_query(user=frappe.session.user).run(as_dict=True)):
		return []

	pos_payment_methods = _get_pos_payment_methods_query([pos_profile['name'] for pos_profile in pos_profiles]).run(as_dict=True)

	payment_methods_by_profile = {}
	for payment_method in pos_payment_methods:
		payment_methods_by_profile.setdefault(payment_method["parent"], []).append(
			{
				"default": payment_method["default"],
				"allow_in_returns": payment_method["allow_in_returns"],
				"mode_of_payment": payment_method["mode_of_payment"],
				"default_account": payment_method["default_account"],
				"account_currency": payment_method["account_currency"],
			}
		)

	for pos_profile in pos_profiles:
		pos_profile["payments"] = payment_methods_by_profile.get(pos_profile["name"], [])

	from pprint import pprint
	pprint(pos_payment_methods)

	return pos_profiles
