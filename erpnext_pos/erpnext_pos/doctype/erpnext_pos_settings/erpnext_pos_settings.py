from frappe.model.document import Document


class ERPNextPOSSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		desktop_oauth_client: DF.Link | None
		mobile_oauth_client: DF.Link | None
	# end: auto-generated types

	pass
