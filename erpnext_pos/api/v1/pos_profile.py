from pypika.queries import QueryBuilder

import frappe


def _get_pos_payment_methods_query(pos_profiles: list[str]):
	"""Return the POS Payment Methods for a POS Profile."""
	pos_payment_method = frappe.qb.DocType('POS Payment Method')

	return (
		frappe.qb.from_(pos_payment_method)
		.where(pos_payment_method.parent.isin(pos_profiles))
		.select(
			pos_payment_method.parent,
			pos_payment_method.default,
			pos_payment_method.allow_in_returns,
			pos_payment_method.mode_of_payment
		)
		.orderby(pos_payment_method.idx)
	)


def _get_user_pos_profiles_query(user: str = frappe.session.user) -> QueryBuilder:
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

	_get_pos_payment_methods_query([pos_profile['name'] for pos_profile in pos_profiles])

	return pos_profiles
