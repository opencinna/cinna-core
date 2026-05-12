"""Typed parser for ``AgentBundleRevision.required_credential_specs`` entries.

Spec dicts arrive as JSON-loaded ``list[dict]`` and multiple service-layer
callers (catalog install context, installer credential setup, template
materialisation) reach into the same shape with the same defensive
``isinstance`` / ``or {}`` / ``or []`` coalescing. This module centralises
that parsing into a single ``ParsedCredentialSpec`` value object so the
call sites consume typed fields instead of re-validating raw dicts.

The matcher in ``CredentialsService.find_match_for_spec`` keeps its raw
``template_data`` + ``template_private_fields`` parameters — the parsed
type belongs to the bundle-spec consumer side, not the credential matcher.
"""
import uuid
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ParsedCredentialSpec:
    name: str
    type: str
    description: str | None
    provided_by: Literal["user", "publisher", "template"]
    publisher_credential_id: uuid.UUID | None
    template_data: dict
    template_private_fields: list[str]

    @property
    def non_private_template_data(self) -> dict:
        """``template_data`` with ``template_private_fields`` keys stripped.

        Useful for comparing against a candidate credential's decrypted data
        (matcher) or seeding a placeholder Credential's encrypted_data
        (materialise).
        """
        private = set(self.template_private_fields)
        return {k: v for k, v in self.template_data.items() if k not in private}


def parse_credential_spec(spec: object) -> ParsedCredentialSpec | None:
    """Parse one entry from ``revision.required_credential_specs``.

    Returns None if the spec is unusable (not a dict, missing name/type).
    """
    if not isinstance(spec, dict):
        return None
    name = spec.get("name")
    type_str = spec.get("type")
    if not name or not type_str:
        return None

    provided_by_raw = spec.get("provided_by") or "user"
    provided_by: Literal["user", "publisher", "template"]
    if provided_by_raw == "publisher":
        provided_by = "publisher"
    elif provided_by_raw == "template":
        provided_by = "template"
    else:
        provided_by = "user"

    publisher_credential_id_raw = spec.get("publisher_credential_id")
    publisher_credential_id: uuid.UUID | None
    if publisher_credential_id_raw is None:
        publisher_credential_id = None
    else:
        try:
            publisher_credential_id = uuid.UUID(str(publisher_credential_id_raw))
        except (ValueError, TypeError):
            publisher_credential_id = None

    template_data_raw = spec.get("template_data") or {}
    template_data = template_data_raw if isinstance(template_data_raw, dict) else {}

    private_fields_raw = spec.get("template_private_fields") or []
    if isinstance(private_fields_raw, list):
        template_private_fields = [f for f in private_fields_raw if isinstance(f, str)]
    else:
        template_private_fields = []

    description_raw = spec.get("description")
    description = description_raw if isinstance(description_raw, str) else None

    return ParsedCredentialSpec(
        name=name,
        type=type_str,
        description=description,
        provided_by=provided_by,
        publisher_credential_id=publisher_credential_id,
        template_data=template_data,
        template_private_fields=template_private_fields,
    )
