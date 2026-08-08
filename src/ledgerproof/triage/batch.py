"""Batch-API plumbing for the frontier sweep.

The Batch API halves the cost, which is why the sweep uses it — but it accepts
raw request params, not the SDK's `messages.parse()` helper. That helper is
what normally converts a Pydantic model into a schema the structured-outputs
API will accept, so here the same transform is done explicitly:

- every object must set `additionalProperties: false`
- every property must appear in `required` (structured outputs has no notion of
  an optional field; a field with a default is still required on the wire)

Getting this wrong does not fail loudly — the API rejects the schema and the
whole batch errors, or worse, a subset of models produce unparseable output and
the comparison quietly becomes a test of JSON formatting rather than diagnosis.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

# Constraint keywords structured outputs rejects outright — a schema carrying
# one returns 400 and takes the whole batch with it. Pydantic emits them from
# ordinary field constraints (`Field(gt=0)` becomes `exclusiveMinimum`), so
# they are stripped here exactly as `messages.parse()` does, and still enforced
# client-side when the response is validated back into the model.
UNSUPPORTED_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
})


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A JSON schema the structured-outputs API will accept."""
    return _strictify(model.model_json_schema())


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {
        key: _strictify(value)
        for key, value in node.items()
        if key not in UNSUPPORTED_KEYWORDS
    }
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        properties = out.get("properties") or {}
        # Structured outputs requires EVERY property in `required`; Pydantic
        # omits ones carrying defaults, which the API rejects.
        out["required"] = sorted(properties)
    return out


def output_config(model: type[BaseModel]) -> dict[str, Any]:
    return {"format": {"type": "json_schema", "schema": strict_schema(model)}}


def parse_verdict(text: str, model: type[BaseModel]):
    """Validate a batched response body against the schema.

    Returns (parsed, error). A model that emits invalid JSON is a result to
    record, not an exception to raise mid-sweep.
    """
    try:
        return model.model_validate(json.loads(text)), None
    except (ValueError, ValidationError) as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300]
