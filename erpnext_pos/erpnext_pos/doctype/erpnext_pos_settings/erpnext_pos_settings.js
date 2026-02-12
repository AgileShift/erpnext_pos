frappe.ui.form.on("ERPNext POS Settings", {
	refresh(frm) {
		frm._pospm_show_advanced = !!frm._pospm_show_advanced;
		render_permission_matrix(frm);
	},
	permission_matrix_doctype(frm) {
		render_permission_matrix(frm);
	},
	permission_matrix_role(frm) {
		render_permission_matrix(frm);
	},
});

const POS_PM_CORE_RIGHTS = ["read", "write", "create", "delete", "submit", "cancel", "print", "export", "report"];
const POS_PM_ADVANCED_RIGHTS = ["select", "amend", "import", "share", "email"];

function humanize_right(ptype) {
	return (ptype || "")
		.replace(/_/g, " ")
		.replace(/\b\w/g, (s) => s.toUpperCase());
}

function get_visible_rights(frm) {
	const rights = [...POS_PM_CORE_RIGHTS];
	if (frm._pospm_show_advanced) {
		rights.push(...POS_PM_ADVANCED_RIGHTS);
	}
	return rights;
}

function ensure_matrix_styles(wrapper) {
	if (wrapper.find("#pos-pm-style").length) return;
	wrapper.append(`
		<style id="pos-pm-style">
			.pos-pm-table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: 8px; }
			.pos-pm-table { width: 100%; border-collapse: collapse; min-width: 980px; }
			.pos-pm-table th, .pos-pm-table td { border-bottom: 1px solid var(--border-color); padding: 8px; text-align: center; }
			.pos-pm-table th { background: var(--subtle-fg); font-size: 12px; white-space: nowrap; }
			.pos-pm-table td.pos-pm-left { text-align: left; }
			.pos-pm-actions { white-space: nowrap; }
			.pos-pm-toolbar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
		</style>
	`);
}

function render_permission_matrix(frm) {
	const wrapper = frm.get_field("permission_matrix_html").$wrapper;
	const doctype = (frm.doc.permission_matrix_doctype || "").trim();
	const role = (frm.doc.permission_matrix_role || "").trim();

	wrapper.empty();
	ensure_matrix_styles(wrapper);

	const toolbar = $(`
		<div class="pos-pm-toolbar">
			<button class="btn btn-sm btn-primary" data-action="add">${__("Add Rule")}</button>
			<button class="btn btn-sm btn-default" data-action="advanced">
				${frm._pospm_show_advanced ? __("Hide Advanced Rights") : __("Show Advanced Rights")}
			</button>
			<button class="btn btn-sm btn-default" data-action="open">${__("Open Core Permission Manager")}</button>
		</div>
	`).appendTo(wrapper);

	const help = $(
		`<div class="text-muted" style="margin-bottom: 10px;">
			${__("Tip: edit rights in a row and click Save. Changes apply to ERPNext Custom DocPerm immediately.")}
		</div>`
	).appendTo(wrapper);
	help.toggle(!!doctype);

	if (!doctype) {
		$(`<div class="text-muted">${__("Select a DocType to manage permissions.")}</div>`).appendTo(wrapper);
		bind_toolbar(frm, toolbar);
		return;
	}

	const container = $(`<div class="pos-pm-table-wrap"><div class="text-muted" style="padding: 10px;">${__("Loading permission matrix...")}</div></div>`).appendTo(wrapper);
	bind_toolbar(frm, toolbar);

	frappe.call({
		method: "erpnext_pos.permission_matrix.get_permission_matrix",
		args: { doctype, role: role || null },
		callback: (r) => {
			const payload = (r && r.message) || {};
			const rows = payload.rows || [];
			frm._pospm_rows = rows;
			render_existing_permissions_hint(wrapper, role, rows);
			draw_matrix_table(frm, container, doctype, rows);
		},
	});
}

function render_existing_permissions_hint(wrapper, role, rows) {
	wrapper.find(".pos-pm-existing-hint").remove();
	if (!role) return;

	let html = "";
	if (!rows.length) {
		html = `<div class="text-muted">${__("No existing rules for role {0} on current DocType.", [frappe.utils.escape_html(role)])}</div>`;
	} else {
		const lines = rows
			.map((row) => {
				const enabled = [...POS_PM_CORE_RIGHTS, ...POS_PM_ADVANCED_RIGHTS]
					.filter((ptype) => row[ptype])
					.map((ptype) => humanize_right(ptype));
				const rightsText = enabled.length ? enabled.join(", ") : __("No rights");
				return `<div>${__("Level")} ${row.permlevel || 0} · ${__("Owner Only")}: ${row.if_owner ? __("Yes") : __("No")} · ${frappe.utils.escape_html(rightsText)}</div>`;
			})
			.join("");
		html = `
			<div style="font-weight: 600; margin-bottom: 4px;">${__("Existing rules for role {0}", [frappe.utils.escape_html(role)])}</div>
			${lines}
		`;
	}

	$(`<div class="pos-pm-existing-hint" style="margin-bottom: 10px; padding: 8px; border: 1px solid var(--border-color); border-radius: 8px;">${html}</div>`).appendTo(wrapper);
}

function bind_toolbar(frm, toolbar) {
	toolbar.find("[data-action='add']").off("click").on("click", () => open_add_rule_dialog(frm));
	toolbar
		.find("[data-action='advanced']")
		.off("click")
		.on("click", () => {
			frm._pospm_show_advanced = !frm._pospm_show_advanced;
			render_permission_matrix(frm);
		});
	toolbar
		.find("[data-action='open']")
		.off("click")
		.on("click", () => {
			const doctype = (frm.doc.permission_matrix_doctype || "").trim();
			if (!doctype) {
				frappe.msgprint(__("Select a DocType first."));
				return;
			}
			frappe.set_route("permission-manager", doctype);
		});
}

function draw_matrix_table(frm, container, doctype, rows) {
	container.empty();
	if (!rows.length) {
		container.html(`<div class="text-muted" style="padding: 10px;">${__("No permission rules for this DocType/filter.")}</div>`);
		return;
	}

	const rights = get_visible_rights(frm);
	const headerCells = rights.map((ptype) => `<th>${frappe.utils.escape_html(humanize_right(ptype))}</th>`).join("");
	const table = $(`
		<table class="pos-pm-table">
			<thead>
				<tr>
					<th>${__("Role")}</th>
					<th>${__("Level")}</th>
					<th>${__("Owner Only")}</th>
					${headerCells}
					<th>${__("Actions")}</th>
				</tr>
			</thead>
			<tbody></tbody>
		</table>
	`);

	const tbody = table.find("tbody");
	rows.forEach((row) => {
		const tr = $(`<tr></tr>`);
		tr.data("row", row);

		tr.append(`<td class="pos-pm-left"><b>${frappe.utils.escape_html(row.role || "")}</b></td>`);
		tr.append(`<td>${row.permlevel || 0}</td>`);
		tr.append(`<td>${row.if_owner ? __("Yes") : __("No")}</td>`);

		rights.forEach((ptype) => {
			const checked = row[ptype] ? "checked" : "";
			tr.append(
				`<td><input type="checkbox" class="pos-pm-right" data-ptype="${frappe.utils.escape_html(ptype)}" ${checked}></td>`
			);
		});

		const actions = $(`
			<td class="pos-pm-actions">
				<button class="btn btn-xs btn-primary" data-action="save">${__("Save")}</button>
				<button class="btn btn-xs btn-danger" data-action="remove">${__("Remove")}</button>
			</td>
		`);
		tr.append(actions);

		actions.find("[data-action='save']").on("click", () => save_matrix_row(frm, doctype, tr));
		actions.find("[data-action='remove']").on("click", () => remove_matrix_row(frm, doctype, row));
		tbody.append(tr);
	});

	container.append(table);
}

function collect_row_rights(tr) {
	const original = tr.data("row") || {};
	const allRights = [...POS_PM_CORE_RIGHTS, ...POS_PM_ADVANCED_RIGHTS];
	const rights = {};
	allRights.forEach((ptype) => {
		const checkbox = tr.find(`input[data-ptype="${ptype}"]`);
		if (checkbox.length) {
			rights[ptype] = checkbox.is(":checked") ? 1 : 0;
		} else {
			rights[ptype] = original[ptype] ? 1 : 0;
		}
	});
	return rights;
}

function save_matrix_row(frm, doctype, tr) {
	const row = tr.data("row") || {};
	const rights = collect_row_rights(tr);
	frappe.call({
		method: "erpnext_pos.permission_matrix.set_matrix_rule",
		args: {
			doctype,
			role: row.role,
			permlevel: row.permlevel || 0,
			if_owner: row.if_owner ? 1 : 0,
			rights,
		},
		callback: () => {
			frappe.show_alert({ message: __("Permissions applied to ERPNext"), indicator: "green" });
			render_permission_matrix(frm);
		},
	});
}

function remove_matrix_row(frm, doctype, row) {
	frappe.confirm(
		__("Remove this permission rule?"),
		() => {
			frappe.call({
				method: "erpnext_pos.permission_matrix.remove_matrix_rule",
				args: {
					doctype,
					role: row.role,
					permlevel: row.permlevel || 0,
					if_owner: row.if_owner ? 1 : 0,
				},
				callback: () => render_permission_matrix(frm),
			});
		},
		() => {}
	);
}

function open_add_rule_dialog(frm) {
	const doctype = (frm.doc.permission_matrix_doctype || "").trim();
	const selectedRole = (frm.doc.permission_matrix_role || "").trim();
	const ifOwner = frm.doc.permission_matrix_if_owner ? 1 : 0;
	if (!doctype) {
		frappe.msgprint(__("Select a DocType first."));
		return;
	}
	if (!selectedRole) {
		frappe.msgprint(__("Select Role Filter first."));
		return;
	}

	const role = selectedRole;
	const currentRows = (frm._pospm_rows || []).filter((row) => row.role === role);
	const duplicate = currentRows.find((row) => (row.permlevel || 0) === 0 && (row.if_owner ? 1 : 0) === ifOwner);
	if (duplicate) {
		const enabled = [...POS_PM_CORE_RIGHTS, ...POS_PM_ADVANCED_RIGHTS]
			.filter((ptype) => duplicate[ptype])
			.map((ptype) => humanize_right(ptype));
		const rightsText = enabled.length ? enabled.join(", ") : __("No rights");
		frappe.msgprint(
			__("Rule already exists for this role with Level 0 and Owner Only = {0}. Current rights: {1}", [
				ifOwner ? __("Yes") : __("No"),
				rightsText,
			])
		);
		return;
	}

	frappe.call({
		method: "erpnext_pos.permission_matrix.add_matrix_rule",
		args: {
			doctype,
			role,
			permlevel: 0,
			if_owner: ifOwner,
		},
		callback: () => render_permission_matrix(frm),
	});
}
