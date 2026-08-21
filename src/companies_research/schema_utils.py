"""Turn a Pydantic model into a schema the structured-outputs API accepts.

The API supports a subset of JSON Schema: no numeric/string/array constraints,
and every object must declare ``additionalProperties: false`` plus a complete
``required`` list. Pydantic emits constraints and omits fields that have
defaults, so we normalise both here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keywords the structured-outputs validator rejects. They are still enforced
# client-side when we validate the response with Pydantic.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)


def _sanitize(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key in UNSUPPORTED_KEYWORDS:
                continue
            # Property names are arbitrary; don't strip keywords inside them.
            cleaned[key] = (
                {k: _sanitize(v) for k, v in value.items()}
                if key == "properties" and isinstance(value, dict)
                else _sanitize(value)
            )
        if cleaned.get("type") == "object" and isinstance(cleaned.get("properties"), dict):
            cleaned["additionalProperties"] = False
            cleaned["required"] = list(cleaned["properties"].keys())
        return cleaned
    if isinstance(node, list):
        return [_sanitize(item) for item in node]
    return node


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return _sanitize(model.model_json_schema())


def output_format_for(model: type[BaseModel]) -> dict[str, Any]:
    """Value for ``output_config["format"]``."""
    return {"type": "json_schema", "schema": json_schema_for(model)}
