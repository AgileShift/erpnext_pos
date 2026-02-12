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
4. `02 - Mutations (Idempotent)/0) pos_session.opening_create_submit`
5. `01 - Bootstrap + Read APIs/1) sync.bootstrap (requires open shift)`
6. Remaining read APIs (`pull_delta`, `inventory`, `customer`)
7. Mutation APIs (`customer.upsert_atomic`, `sales_invoice.create_submit`, `payment_entry.create_submit`, `closing_create_submit`, `cancel`)

## Access and Filters
- `currencies` in `sync.bootstrap` come from enabled `Currency` records in the site (`enabled=1`) and include `exchange_rate` (to base company currency).
- `exchange_rates` in `sync.bootstrap` returns a map (`base_currency`, `date`, `rates`) for quick lookup in mobile.
- `sync.my_pos_profiles` returns only POS Profiles assigned to authenticated user through `POS Profile.applicable_for_users` (`POS Profile User` rows).
- `sync.bootstrap` enforces open shift (`POS Opening Entry` in status `Open`) before returning context.
- Inventory is filtered by `warehouse`.
- Customers are filtered by:
  - `route` when `Customer.route` exists in that site.
  - otherwise by `territory` (`Customer.territory`).
- `pos_session.opening_create_submit` returns the existing `POS Opening Entry` (`reused=true`) only when there is already an open shift for the same authenticated user (and profile context), never from another user.

## Notes
- All endpoints are protected except `discovery.resolve_site`.
- Mutation endpoints support `client_request_id` (recommended for client tracing).  
  If omitted, API generates deterministic fallback `request_id` (`<user>:<payload_hash>`) and still applies idempotency.
- All v1 endpoints now return a uniform envelope for both success and error:
  - `success`, `data`, `error`, `request_id`, `server_time`
- `discovery.resolve_site` returns `runtime_defaults` only (no `flow`, `endpoints`, `opening_defaults`).
- `pos_session.opening_create_submit` supports minimal payload: server infers `user`, `company`, `posting_date`, `period_start_date`, and `balance_details` when omitted.
- For non-base currencies, `exchange_rate` can be `null` if no local `Currency Exchange` exists and ERPNext cannot resolve a rate from its configured exchange source.

- `sync.pull_delta` returns DTO-ready payloads per doctype:
  - `changes.Inventory`: consolidated `WarehouseItemDto` shape (aliases `Bin`/`Item`/`Item Price` normalize here).
  - `changes.Customer`: `CustomerDto` shape with `credit_limits`.
  - `changes.Sales Invoice`: sales invoice header + `items` + `payments` + `payment_schedule`.
  - `changes.Payment Entry`: payment entry header + `references`.
- Inventory rows now include barcode and variant metadata (`variant_of`, `variant_attributes`) while keeping backward compatibility with existing app DTOs.
- Inventory now returns all active sales items (`is_sales_item=1`, `disabled=0`) for the requested warehouse/profile context; stock rows without `Bin` are returned with `actual_qty=0`.
- Inventory alerts include both camel and snake aliases for item keys (`itemCode` + `item_code`, `itemName` + `item_name`).
- API accepts both `snake_case` and `camelCase` in the main request payload keys (`profile_name/profileName`, `price_list/priceList`, etc.) to minimize mobile-side changes.
