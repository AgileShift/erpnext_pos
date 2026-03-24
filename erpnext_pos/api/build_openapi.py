# erpnext_pos/scripts/build_openapi.py
from __future__ import annotations

import inspect
import yaml

from apispec import APISpec
from erpnext_pos.api.v1 import discovery


def load_operation_from_docstring(fn):
    doc = inspect.getdoc(fn) or ""
    if "---" not in doc:
        return None

    raw = doc.split("---", 1)[1].rsplit("---", 1)[0]
    return yaml.safe_load(raw)


def build_spec():
    spec = APISpec(
        title="ERPNext POS API",
        version="1.0.0",
        openapi_version="3.1.0",
        info={"description": "Public API for ERPNext POS"},
    )

    endpoints = [
        (
            "/api/v2/method/erpnext_pos.api.v1.discovery.resolve_site",
            "get",
            discovery.resolve_site,
        ),
    ]

    for path, method, fn in endpoints:
        operation = load_operation_from_docstring(fn)
        if operation:
            spec.path(path=path, operations={method: operation})

    return spec


if __name__ == "__main__":
    spec = build_spec()
    print(spec.to_yaml())
