from typing import Any

from scripts.check_openapi_compat import incompatibilities


def test_removed_route_is_breaking() -> None:
    baseline: dict[str, Any] = {"paths": {"/one": {"get": {"responses": {"200": {}}}}}}
    assert incompatibilities(baseline, {"paths": {}}) == ["removed path: /one"]


def test_added_route_is_compatible() -> None:
    baseline: dict[str, Any] = {"paths": {"/one": {"get": {"responses": {"200": {}}}}}}
    candidate: dict[str, Any] = {
        "paths": {**baseline["paths"], "/two": {"get": {"responses": {"200": {}}}}},
    }
    assert incompatibilities(baseline, candidate) == []


def test_added_required_body_field_is_breaking() -> None:
    baseline = {
        "paths": {"/one": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}}, "responses": {"200": {}}}}},
        "components": {"schemas": {"Body": {"type": "object", "required": ["one"]}}},
    }
    candidate = {
        "paths": baseline["paths"],
        "components": {"schemas": {"Body": {"type": "object", "required": ["one", "two"]}}},
    }
    assert incompatibilities(baseline, candidate) == ["new required request field two: POST /one"]
