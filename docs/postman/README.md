# ERPNext POS API v1 - Postman Test Guide

Base URL (local): `http://localhost:8000`

Collection:
- `erpnext_pos_v1_localhost.postman_collection.json`

Environment:
- `erpnext_pos_v1_localhost.postman_environment.json`

## Test Flow (recommended)
1. `00 - Discovery + OAuth/1) Discover Site (public)`
2. OAuth requests (`Authorize` -> `Exchange Code` or `Refresh Token`)
3. `01 - Bootstrap + Read APIs/0) sync.my_pos_profiles`
4. `01 - Bootstrap + Read APIs/0.1) sync.pos_profile_detail`
5. `02 - Mutations (Idempotent)/0) pos_session.opening_create_submit`
6. `01 - Bootstrap + Read APIs/1) sync.bootstrap (requires open shift)`
7. Remaining read APIs (`pull_delta`, `activity.pull`, `inventory`, `customer`, `sales_invoice.print_*`)
8. `03 - Settings APIs/1) settings.mobile_get`
9. `03 - Settings APIs/2) settings.mobile_update`
10. Mutation APIs (`customer.upsert_atomic`, `sales_invoice.create_submit`, `payment_entry.create_submit`, `closing_create_submit`, `cancel`)

## Access and Filters
- `currencies` in `sync.bootstrap` come from enabled `Currency` records in the site (`enabled=1`) and include `exchange_rate` (to base company currency).
- `exchange_rates` in `sync.bootstrap` returns a map (`base_currency`, `date`, `rates`) for quick lookup in mobile.
- `sync.my_pos_profiles` returns only POS Profiles assigned to authenticated user through `POS Profile.applicable_for_users` (`POS Profile User` rows).
- `sync.pos_profile_detail` returns the selected POS Profile detail (for authenticated user access), including `payments` with the compact method shape: `mode_of_payment`, `allow_in_returns`, `account`, `currency`, `type`, `default`.
- `sync.bootstrap` enforces open shift (`POS Opening Entry` in status `Open`) before returning context.
- `sync.bootstrap` now returns `pos_profiles` as full detail objects (same shape as `pos_profile_detail`) and no longer includes a top-level `pos_profile_detail` key.
- `sync.bootstrap` ahora pagina colecciones grandes y devuelve wrappers:
  inventory/customers/invoices/payment_entries/activity -> `{ items: [...], pagination: { offset, limit, total, has_more } }`
- `sync.bootstrap` includes invoices that either match the POS profile or have `pos_profile` empty, scoped to the active company context.
- `sync.bootstrap` invoice rules are fixed:
  - pending invoices: last 90 days
  - `Cancelled`, `Return`, `Credit Note Issued`: last 7 days
- Inventory is filtered by `warehouse`.
- Customers are filtered by:
  - `route` when `Customer.route` exists in that site.
  - otherwise by `territory` (`Customer.territory`).
- `pos_session.opening_create_submit` returns the existing `POS Opening Entry` (`reused=true`) only when there is already an open shift for the same authenticated user (and profile context), never from another user.

## Notes
- All endpoints are protected except `discovery.resolve_site`.
- All mutation and read payloads documented in this collection use only `snake_case`.
- All v1 endpoints return a uniform envelope for both success and error:
  - `success`, `data`, `error`, `server_time`
- `discovery.resolve_site` returns `runtime_defaults` only (no `flow`, `endpoints`, `opening_defaults`).
- `pos_session.opening_create_submit` supports minimal payload: server infers `user`, `company`, `posting_date`, `period_start_date`, and `balance_details` when omitted.
- For non-base currencies, `exchange_rate` can be `null` if no local `Currency Exchange` exists and ERPNext cannot resolve a rate from its configured exchange source.

- `sync.pull_delta` returns DTO-ready payloads per doctype:
  - `changes.Inventory`: consolidated `WarehouseItemDto` shape (aliases `Bin`/`Item`/`Item Price` normalize here), now enriched per item with:
    - `has_stock_alert`
    - `stock_alert_status`
    - `stock_alert_qty`
    - `stock_alert_reorder_level`
    - `stock_alert_reorder_qty`
    - `stock_alert` (object with alert detail or `null`)
  - `changes.Customer`: `CustomerDto` shape with `credit_limits`.
  - `changes.Sales Invoice`: sales invoice header + `items` + `payments` + `payment_schedule`.
  - `changes.Payment Entry`: payment entry header + `references`.
  - `changes.Activity`: cashier activity feed (`Customer`, `Sales Invoice`, `Payment Entry`) for in-app notifications.
- `activity.pull` is a direct notifications endpoint (same event schema as `changes.Activity`) and supports filters:
  - `only_other_cashiers` (default `true`)
  - `event_types` (example: `["Customer", "Sales Invoice", "Payment Entry"]`)
  - POS context filters: `company`, `pos_profile/profile_name`, `warehouse`, `route`, `territory`
- `inventory.list_with_alerts` now returns only `alerts` (no `items`) to avoid duplicate inventory payloads.
- Inventory rows now include barcode and variant metadata (`variant_of`, `variant_attributes`) while keeping backward compatibility with existing app DTOs.
- Inventory now returns all active sales items (`is_sales_item=1`, `disabled=0`) for the requested warehouse/profile context; stock rows without `Bin` are returned with `actual_qty=0`.
- Inventory alerts use only `snake_case` item keys.
- `sync.bootstrap` no longer returns `payment_methods`; use `pos_profiles[].payments`.
- `settings.mobile_get` returns centralized POS API settings + optional options catalog (`roles/users/warehouses/item_groups`) for a mobile settings screen.
- `settings.mobile_update` applies those settings atomically (single + tables): allowed API roles/users, user-role bindings and inventory alert rules.
- `sales_invoice.create_submit` and `payment_entry.create_submit` return compact submit summaries (`name`, `docstatus`, totals, `modified`) compatible with sync mapping.
- `customer.upsert_atomic` now performs true upsert (create or update existing by id/name/mobile match) and can update linked primary contact/address.
- `sales_invoice.print_options` returns available print formats for `Sales Invoice` and resolves default/selected format.
- `sales_invoice.print_html` returns rendered HTML using the configured/default print format (for in-app print preview).
- `sales_invoice.print_pdf` returns PDF as:
  - `response_mode=base64` (default): `pdf_base64` for direct download/print in mobile.
  - `response_mode=file_url`: creates `File` attached to invoice and returns `file.file_url`.
  - `response_mode=both`: returns both.
  - optional `pdf_generator`: `wkhtmltopdf` or `chrome`.
  - if `wkhtmltopdf` is missing, API retries automatically with `chrome` and returns `pdf_generator` used in response.
