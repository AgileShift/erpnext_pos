# Flujo API v1 y cobertura real contra app móvil

## Resumen ejecutivo
La API v1 ya soporta los flujos críticos que usa la app POS (apertura de caja, bootstrap, sync delta, clientes, inventario, facturas, pagos y cierre), manteniendo respuesta uniforme:
- `success`
- `data`
- `error`
- `request_id`
- `server_time`

## Flujo operativo esperado
1. `discovery.resolve_site` (sin auth)
2. OAuth login
3. `sync.my_pos_profiles`
4. `pos_session.opening_create_submit`
5. `sync.bootstrap`
6. `sync.pull_delta` (recurrente)
7. Mutaciones atómicas (`customer.upsert_atomic`, `sales_invoice.create_submit`, `payment_entry.create_submit`, `pos_session.closing_create_submit`)

## Mapa de cobertura (app -> API v1)

### Apertura/cierre de turno
- App usa: open shift, validar shift abierto, cierre.
- API v1:
  - `pos_session.opening_create_submit`
  - `pos_session.closing_create_submit`
- Estado: cubierto.

### Contexto inicial POS
- App usa: perfiles POS, bodegas, monedas, métodos de pago, datos base.
- API v1:
  - `sync.my_pos_profiles`
  - `sync.bootstrap`
- Estado: cubierto.

### Inventario consolidado
- App usa catálogo consolidado por bodega + alertas de stock.
- API v1:
  - `sync.bootstrap` (inventario inicial)
  - `sync.pull_delta` -> `changes.Inventory`
  - `inventory.list_with_alerts` (solo alertas)
- Estado: cubierto.

### Clientes y cartera
- App usa: lista de clientes, outstanding, altas/actualizaciones.
- API v1:
  - `sync.bootstrap` (clientes iniciales)
  - `sync.pull_delta` -> `changes.Customer`
  - `customer.outstanding`
  - `customer.upsert_atomic`
- Estado: cubierto.

### Facturación y pagos
- App usa: crear factura, submit, crear payment entry, submit, cancelaciones.
- API v1:
  - `sales_invoice.create_submit`
  - `payment_entry.create_submit`
  - `sales_invoice.cancel`
- Estado: cubierto.

### Impresión
- App usa: vista e impresión de factura.
- API v1:
  - `sales_invoice.print_options`
  - `sales_invoice.print_html`
  - `sales_invoice.print_pdf`
- Estado: cubierto (PDF depende de motor instalado).

### Notificaciones de actividad (cashiers)
- App usa: feed de actividad para interacción entre cajeros.
- API v1:
  - `activity.pull`
  - `sync.pull_delta` -> `changes.Activity`
- Estado: cubierto.

## Hallazgos relevantes del recorrido en app KMP
- La app actual todavía contiene consumo directo de endpoints ERP para varios recursos en `APIService.kt`.
- La migración hacia API v1 puede hacerse con cambios mínimos si se centraliza en un adaptador de endpoint base.
- Las preferencias de Settings de la app (tema, idioma, sync local, etc.) son locales; no deben mezclarse con seguridad/permisos del servidor.

Referencias revisadas:
- `/Users/herrold/Desktop/AgileShift/ERP-POS/erpnext_pos_kmp/composeApp/src/commonMain/kotlin/com/erpnext/pos/remoteSource/api/APIService.kt`
- `/Users/herrold/Desktop/AgileShift/ERP-POS/erpnext_pos_kmp/composeApp/src/commonMain/kotlin/com/erpnext/pos/data/repositories/InventoryAlertRepository.kt`
- `/Users/herrold/Desktop/AgileShift/ERP-POS/erpnext_pos_kmp/composeApp/src/commonMain/kotlin/com/erpnext/pos/data/repositories/CustomerRepository.kt`

## Riesgos/puntos operativos pendientes
- PDF requiere motor disponible (`wkhtmltopdf` o fallback `chrome` según entorno).
- Si el sitio tiene personalizaciones fuertes de permisos, validar `Permission Manager` en staging antes de producción.
- Si existen flujos legacy directos aún activos en app, priorizar migrarlos al endpoint atómico correspondiente.
