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
  - validar acceso (`enforce_api_access`)
  - exponer endpoints `settings.mobile_get` y `settings.mobile_update`
  - manejar tablas hijas (roles, usuarios, bindings, reglas DocType, alertas inventario)

### 3) Aplicación de permisos reales al core
- Archivo: `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/access.py`
- Función:
  - convertir configuración en permisos reales (`Custom DocPerm`)
  - asignar roles faltantes a usuarios
  - mantener fallback de permisos mínimos para POS móvil

### 4) Single de configuración Desk
- Archivos:
  - `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/erpnext_pos/doctype/erpnext_pos_settings/erpnext_pos_settings.json`
  - `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/erpnext_pos/doctype/erpnext_pos_settings/erpnext_pos_settings.py`
  - `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/erpnext_pos/doctype/erpnext_pos_settings/erpnext_pos_settings.js`
- Función:
  - un único formulario para toda la configuración POS
  - validaciones estrictas en servidor
  - edición directa de reglas sin diálogo/matriz compleja

### 5) Endpoints de negocio
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
- Idempotencia para operaciones POST sensibles.
- Respuesta uniforme para robustez cliente.
- Configuración de acceso/permisos centralizada en un único Single.
- Compatibilidad con sitios legacy (campo CSV de roles como fallback).

## Ciclo de instalación y migración
- Archivo: `/Users/herrold/Desktop/Personal/IR/ERP/erp/apps/erpnext_pos/erpnext_pos/install.py`
- En `after_install` y `after_migrate`:
  - aplica defaults del Single
  - asegura módulo/workspace `POS Mobile`
  - inicializa controles de acceso

## Qué revisar en cada despliegue
1. `bench --site <sitio> migrate`
2. Verificar en Desk: `POS Mobile > ERPNext POS Settings`
3. Confirmar `enable_api` y reglas de acceso
4. Probar colección Postman v1 de punta a punta
