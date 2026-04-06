# Arquitectura técnica del código API v1 (ERPNext POS)

## Capas principales

### 1) Seguridad y guard global
- Archivo: `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/guard.py`
- Función:
  - interceptar llamadas `erpnext_pos.api.v1.*`
  - permitir solo `discovery.resolve_site` sin login
  - exigir API habilitada y usuario autorizado para el resto

### 2) Configuración central
- Archivo: `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/settings.py`
- Función:
  - leer settings efectivos (`get_settings`)
  - exponer endpoints `settings.mobile_get` y `settings.mobile_update`
  - exponer solo contrato `snake_case`
  - leer/escribir campos del Single y tablas hijas cuando existan en el sitio

### 3) Single de configuración Desk
- Archivos:
  - `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/erpnext_pos/doctype/erpnext_pos_settings/erpnext_pos_settings.json`
  - `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/erpnext_pos/doctype/erpnext_pos_settings/erpnext_pos_settings.js`
- Función:
  - un único formulario para toda la configuración POS
  - mantener configuración operativa mínima
  - evitar duplicar contratos o reglas de payload en la UI

### 4) Endpoints de negocio
- Ubicación: `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/api/v1/`
- Módulos clave:
  - `discovery.py`: resolución de instancia OAuth y defaults
  - `sync.py`: bootstrap + delta
  - `inventory.py`: inventario consolidado + alertas
  - `customer.py`: clientes/outstanding/upsert
  - `sales_invoice.py`: create/submit/cancel + impresión
  - `payment_entry.py`: create/submit
  - `pos_session.py`: apertura/cierre atómicos
  - `activity.py`: feed de actividad entre cajeros

## Principios aplicados
- Endpoints atómicos para mutaciones críticas.
- Respuesta uniforme para robustez cliente.
- Contrato único en `snake_case` para request y response.
- Helpers transversales mínimos: parseo de payload y envelope de respuesta.
- Configuración centralizada en un único Single, sin matrices paralelas de permisos.

## Estado operativo actual
- No hay `install.py` activo en esta app.
- `after_install` y `after_migrate` están comentados en `hooks.py`.
- La configuración y fixtures se validan vía migraciones estándar y pruebas manuales de endpoints.

## Qué revisar en cada despliegue
1. `bench --site <sitio> migrate`
2. Verificar en Desk: `POS Mobile > ERPNext POS Settings`
3. Confirmar que el contrato usado por cliente/Postman sea `snake_case`
4. Probar colección Postman v1 de punta a punta
