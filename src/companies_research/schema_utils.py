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


class UnsupportedSchema(TypeError):
    """A Pydantic model that structured outputs cannot enforce."""


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
        if cleaned.get("type") == "object":
            if isinstance(cleaned.get("properties"), dict):
                cleaned["additionalProperties"] = False
                cleaned["required"] = list(cleaned["properties"].keys())
            elif isinstance(cleaned.get("additionalProperties"), dict):
                # An open-ended mapping — `dict[str, X]` in Pydantic. The API
                # rejects any additionalProperties that is not false, so fail
                # here with a message that names the fix rather than at the
                # API with one that does not.
                raise UnsupportedSchema(
                    "structured outputs cannot express an open-ended object "
                    "(dict[str, ...]); model it as a list of {key, value} objects"
                )
        return cleaned
    if isinstance(node, list):
        return [_sanitize(item) for item in node]
    return node


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return _sanitize(model.model_json_schema())
