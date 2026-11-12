"""``CAPABILITY_REQUEST_BODY_SHAPE`` (the 422 ``detail.expected``) stays in step
with ``AgentApiCapabilityRequestCreate``: same keys, every nested field named,
and the model's numeric bounds quoted."""

from app.models.improvement.agent_improvement_request import (
    CAPABILITY_REQUEST_BODY_SHAPE,
    AgentApiCapabilityRequestCreate,
    CapabilityRequestParam,
    CapabilityRequestProposedEndpoint,
    CapabilityRequestResponseField,
)


def _field_names(model: type) -> set[str]:
    return {f.alias or name for name, f in model.model_fields.items()}


def _bounds(prop: dict) -> list[int]:
    """Numeric bounds of a JSON-schema property (``anyOf`` for optionals)."""
    branches = prop.get("anyOf", [prop])
    keys = ("minLength", "maxLength", "maxItems")
    return [
        b[k]
        for b in branches
        for k in keys
        if k in b and not (k == "minLength" and b[k] == 0)
    ]


def test_top_level_keys_match_the_model():
    assert set(CAPABILITY_REQUEST_BODY_SHAPE) == set(
        AgentApiCapabilityRequestCreate.model_fields
    )


def test_every_nested_field_is_named():
    nested = CAPABILITY_REQUEST_BODY_SHAPE["proposed_endpoint"]
    for model in (
        CapabilityRequestProposedEndpoint,
        CapabilityRequestParam,
        CapabilityRequestResponseField,
    ):
        for name in _field_names(model):
            assert name in nested, f"{model.__name__}.{name} missing from the shape"


def test_numeric_bounds_match_the_schema():
    top = AgentApiCapabilityRequestCreate.model_json_schema(by_alias=True)["properties"]
    for name, prop in top.items():
        if name == "proposed_endpoint":
            continue
        for bound in _bounds(prop):
            assert str(bound) in CAPABILITY_REQUEST_BODY_SHAPE[name], (name, bound)

    endpoint = CapabilityRequestProposedEndpoint.model_json_schema(by_alias=True)
    nested = CAPABILITY_REQUEST_BODY_SHAPE["proposed_endpoint"]
    for name, prop in endpoint["properties"].items():
        for bound in _bounds(prop):
            assert f"<={bound}" in nested, (name, bound)
