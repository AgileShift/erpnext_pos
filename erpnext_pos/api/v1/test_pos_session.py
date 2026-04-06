import frappe
from frappe.tests import IntegrationTestCase

from erpnext.accounts.doctype.pos_closing_entry.test_pos_closing_entry import init_user_and_profile
from erpnext.accounts.doctype.pos_opening_entry.test_pos_opening_entry import create_opening_entry

from erpnext_pos.api.v1.pos_session import closing_create_submit, opening_create_submit


class TestPosSession(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(cls.change_settings("POS Settings", {"invoice_type": "POS Invoice"}))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.sql("delete from `tabPOS Closing Entry`")
		frappe.db.sql("delete from `tabPOS Opening Entry`")
		frappe.db.sql("delete from `tabPOS Profile`")

	def test_opening_create_submit_supports_minimal_payload(self):
		test_user, pos_profile = init_user_and_profile()

		response = opening_create_submit({"pos_profile": pos_profile.name, "opening_amount": 0})

		self.assertTrue(response["success"])
		self.assertFalse(response["data"]["reused"])
		self.assertEqual(
			frappe.db.get_value("POS Opening Entry", response["data"]["name"], "pos_profile"),
			pos_profile.name,
		)
		self.assertEqual(
			frappe.db.get_value("POS Opening Entry", response["data"]["name"], "user"),
			test_user.name,
		)

	def test_closing_create_submit_accepts_object_rows(self):
		test_user, pos_profile = init_user_and_profile()
		opening_entry = create_opening_entry(pos_profile, test_user.name, get_obj=True)

		response = closing_create_submit(
			{
				"pos_opening_entry": opening_entry.name,
				"user": test_user.name,
				"payment_reconciliation": [{"mode_of_payment": "Cash", "closing_amount": 125}],
			}
		)

		self.assertTrue(response["success"])
		self.assertTrue(frappe.db.exists("POS Closing Entry", response["data"]["name"]))

	def test_closing_create_submit_accepts_pair_rows(self):
		test_user, pos_profile = init_user_and_profile()
		opening_entry = create_opening_entry(pos_profile, test_user.name, get_obj=True)

		response = closing_create_submit(
			{
				"pos_opening_entry": opening_entry.name,
				"user": test_user.name,
				"payment_reconciliation": [["Cash", 75]],
			}
		)

		self.assertTrue(response["success"])
		self.assertTrue(frappe.db.exists("POS Closing Entry", response["data"]["name"]))
