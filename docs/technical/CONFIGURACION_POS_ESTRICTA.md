# Configuración POS estricta (ERPNext POS Settings)

## Objetivo
Definir un único formulario de configuración, con solo campos que realmente impactan el comportamiento de la API v1 y el acceso real en ERPNext.

## Auditoría realizada
Se revisó:
- Backend API: `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos`
- App móvil KMP: `/Users/herrold/Desktop/AgileShift/ERP-POS/erpnext_pos_kmp`

Hallazgos clave:
- La pantalla Settings de la app móvil es mayormente local (preferencias de UI/sync), no controla seguridad/permisos del servidor.
- La configuración de servidor necesaria sí vive en `ERPNext POS Settings` y en endpoints `settings.mobile_get/mobile_update`.
- La matriz visual de permisos (`permission_matrix_*`) generaba complejidad y duplicidad.

## Configuración final que se mantiene

### 1) API y descubrimiento
- `enable_api`: habilita/bloquea toda la API v1 protegida.
- `allow_discovery`: habilita/bloquea `discovery.resolve_site` (único endpoint público).
- `allow_client_secret_response`: controla si discovery devuelve `client_secret`.
- `api_version`: solo lectura (`v1`).

Uso en código:
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/guard.py`
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/discovery.py`
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/settings.py`

### 2) Control de acceso API
- `allowed_api_roles_table`: roles permitidos para invocar endpoints protegidos.
- `allowed_api_users`: allow-list explícita de usuarios.
- `user_role_bindings`: asignación dinámica Usuario -> Rol desde un solo formulario.

Uso en código:
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/access.py`
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/settings.py`

### 3) Parámetros de sincronización
- `default_sync_page_size`
- `bootstrap_invoice_days`
- `recent_paid_invoice_days`

Uso en código:
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/sync.py`

### 4) Alertas de inventario
- `enable_inventory_alerts`
- `inventory_alert_default_limit`
- `inventory_alert_critical_ratio`
- `inventory_alert_low_ratio`
- `inventory_alert_rules` (por warehouse/item_group)

Reglas de cálculo:
- Fuente de cantidad: `projected_qty` (fallback `actual_qty`).
- `CRITICAL` si `qty <= 0`.
- Si existe `reorder_level`:
  - `CRITICAL` si `qty <= reorder_level * critical_ratio`
  - `LOW` si `qty <= reorder_level * low_ratio`
- Precedencia de reglas: primero por `priority` (menor primero), luego reglas específicas de `item_group`, luego comodines.
- Validación estricta: `low_ratio >= critical_ratio`, `priority >= 0`, sin duplicados por combinación `warehouse + item_group`.

Uso en código:
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/inventory.py`
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/sync.py`
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/settings.py`

## Campos eliminados de la UI (por no aportar valor operativo)
Se removieron del formulario:
- `permission_matrix_doctype`
- `permission_matrix_role`
- `permission_matrix_if_owner`
- `permission_matrix_html`
- `doctype_permission_rules`

Motivo:
- duplicaban el Permission Manager,
- agregaban diálogos innecesarios,
- no eran estrictos para operación POS.

## Contrato actualizado de Settings API
`settings.mobile_get` y `settings.mobile_update` ahora incluyen:
- catálogo opcional `options.roles/users/warehouses/item_groups`

Permisos por DocType:
- se administran directamente en `Permission Manager` de ERPNext.
- el formulario `ERPNext POS Settings` ya no incluye una tabla paralela de reglas DocType.

Postman actualizado en:
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/docs/postman/erpnext_pos_v1_localhost.postman_collection.json`
- `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/docs/postman/README.md`
