"""Validate live API responses against the committed OpenAPI spec (contract tests)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).resolve().parents[1] / "ytcore/approvals/service/openapi.json"
SPEC: dict[str, Any] = json.loads(SPEC_PATH.read_text())


def response_schema(path: str, method: str, status: int = 200) -> dict[str, Any]:
    op = SPEC["paths"][path][method.lower()]
    content = op["responses"][str(status)]["content"]["application/json"]
    return dict(content["schema"])


def validate(path: str, method: str, body: Any, status: int = 200) -> None:
    schema = {**response_schema(path, method, status), "components": SPEC["components"]}
    Draft202012Validator(schema).validate(body)
