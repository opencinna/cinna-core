"""The ``required_credential_specs`` entry schema: its single writer and reader.

A credential spec is the frozen, non-secret description of one credential a
published revision needs — ``AgentBundleRevision.required_credential_specs``
and ``SkillPackageRevision.required_credential_specs`` share the schema.

* :func:`build_spec` is the **single writer**. Bundle publish and skill publish
  both call it, so the two revision kinds cannot drift apart in key names, key
  order or how ``provided_by`` maps onto ``publisher_credential_id`` and the
  template payload.
* :func:`parse_credential_spec` is the **single reader**. Spec dicts arrive as
  JSON-loaded ``list[dict]`` and multiple service-layer callers (catalog install
  context, installer credential setup, template materialisation) reach into the
  same shape; this centralises the defensive ``isinstance`` / ``or {}`` /
  ``or []`` coalescing into a ``ParsedCredentialSpec`` value object so the call
  sites consume typed fields instead of re-validating raw dicts.

The matcher in ``CredentialsService.find_match_for_spec`` keeps its raw
``template_data`` + ``template_private_fields`` parameters — the parsed
type belongs to the spec consumer side, not the credential matcher.
"""
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlmodel import Session

from app.models.credentials.credential import Credential


@dataclass(frozen=True)
class ParsedCredentialSpec:
    name: str
    type: str
    description: str | None
    provided_by: Literal["user", "publisher", "template"]
    publisher_credential_id: uuid.UUID | None
    template_data: dict
    template_private_fields: list[str]
    service_uri: str | None
    #: The producer agent an ``agent_api`` slot connects to. Written only by
    #: skill publish (never by bundle publish), so bundle revisions and old
    #: revision JSON carry no key → ``None``.
    producer_agent_id: uuid.UUID | None = None
    #: Its display name as it read at publish, so a reader can say "backed by
    #: agent X" without resolving the id (which a catalog listing would have to
    #: do once per revision). Frozen with the rest of the spec: a later rename
    #: does not reach a published revision, and neither does a deletion.
    producer_agent_name: str | None = None

    @property
    def non_private_template_data(self) -> dict:
        """``template_data`` with ``template_private_fields`` keys stripped.

        Useful for comparing against a candidate credential's decrypted data
        (matcher) or seeding a placeholder Credential's encrypted_data
        (materialise).
        """
        private = set(self.template_private_fields)
        return {k: v for k, v in self.template_data.items() if k not in private}


def build_spec(
    session: Session,
    *,
    credential: Credential | None,
    credential_type: str,
    provided_by: Literal["user", "publisher", "template"],
    name: str,
    service_uri: str | None,
    description: str | None,
) -> dict:
    """Build one ``required_credential_specs`` entry.

    Key order is part of the contract (revision JSON is compared byte for byte
    by bundle consumers): ``name``, ``type``, ``allow_sharing``,
    ``allow_template_sharing``, ``description``, ``provided_by``,
    ``publisher_credential_id``, ``service_uri``, then ``template_data`` and
    ``template_private_fields`` only for ``provided_by="template"``. Callers
    that add a key (skill publish adds ``producer_agent_id`` and
    ``producer_agent_name``) append it after.

    ``credential`` is the publisher's credential the spec was resolved from, or
    ``None`` when there is none to point at (a ``user`` spec). The consent flags
    are copied from it and default to ``False`` without one.

    Raises:
        ValueError: when ``provided_by`` needs a credential and none was given,
            or when a template credential's stored data cannot be decrypted
            (raised by ``PublishService._template_payload_for``).
    """
    if provided_by in ("publisher", "template") and credential is None:
        raise ValueError(
            f"A '{provided_by}' credential spec for '{name}' needs the "
            "credential it is resolved from."
        )

    spec: dict = {
        "name": name,
        "type": credential_type,
        "allow_sharing": (
            bool(credential.allow_sharing) if credential is not None else False
        ),
        "allow_template_sharing": (
            bool(getattr(credential, "allow_template_sharing", False))
            if credential is not None
            else False
        ),
        "description": description,
        "provided_by": provided_by,
        "publisher_credential_id": (
            str(credential.id)
            if provided_by == "publisher" and credential is not None
            else None
        ),
        "service_uri": service_uri,
    }
    if provided_by == "template" and credential is not None:
        # Lazy: publish_service imports this module at load time.
        from app.services.bundles.publish_service import PublishService

        template_data, template_private_fields = (
            PublishService._template_payload_for(session, credential)
        )
        spec["template_data"] = template_data
        spec["template_private_fields"] = template_private_fields
    return spec


def _optional_uuid(raw: object) -> uuid.UUID | None:
    """Read a stored id tolerantly: absent or malformed → ``None``."""
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


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

    publisher_credential_id = _optional_uuid(spec.get("publisher_credential_id"))

    template_data_raw = spec.get("template_data") or {}
    template_data = template_data_raw if isinstance(template_data_raw, dict) else {}

    private_fields_raw = spec.get("template_private_fields") or []
    if isinstance(private_fields_raw, list):
        template_private_fields = [f for f in private_fields_raw if isinstance(f, str)]
    else:
        template_private_fields = []

    description_raw = spec.get("description")
    description = description_raw if isinstance(description_raw, str) else None

    # Non-secret audience/slot id. Old revision JSON has no key → None (I5).
    service_uri_raw = spec.get("service_uri")
    service_uri = service_uri_raw if isinstance(service_uri_raw, str) else None

    producer_name_raw = spec.get("producer_agent_name")
    producer_agent_name = (
        producer_name_raw if isinstance(producer_name_raw, str) else None
    )

    return ParsedCredentialSpec(
        name=name,
        type=type_str,
        description=description,
        provided_by=provided_by,
        publisher_credential_id=publisher_credential_id,
        template_data=template_data,
        template_private_fields=template_private_fields,
        service_uri=service_uri,
        producer_agent_id=_optional_uuid(spec.get("producer_agent_id")),
        producer_agent_name=producer_agent_name,
    )


#: The spec keys a catalog listing may show: what labels a slot and who fills
#: it. ``template_data`` / ``template_private_fields`` (the publisher's shipped
#: field values) and the consent flags stay server-side; the install flows read
#: them off the revision row, never off a listing.
_LISTING_SPEC_KEYS = (
    "name",
    "type",
    "description",
    "provided_by",
    "publisher_credential_id",
    "service_uri",
    "producer_agent_id",
    "producer_agent_name",
)


def listing_credential_specs(specs: object) -> list[dict]:
    """Project ``required_credential_specs`` for a catalog listing.

    Keeps only :data:`_LISTING_SPEC_KEYS` (absent keys stay absent) and drops
    entries that are not dicts. Everything a catalog response carries about a
    credential slot goes through here.

    A ``service_uri`` the publisher marked private (listed in the spec's
    ``template_private_fields``) is omitted, as the install context and
    ``CredentialProvisioner._materialise_template`` already hide it.
    """
    if not isinstance(specs, list):
        return []
    return [_listing_spec(spec) for spec in specs if isinstance(spec, dict)]


def _listing_spec(spec: dict) -> dict:
    private = spec.get("template_private_fields")
    hidden = (
        {"service_uri"}
        if isinstance(private, list) and "service_uri" in private
        else set()
    )
    return {
        key: spec[key]
        for key in _LISTING_SPEC_KEYS
        if key in spec and key not in hidden
    }
