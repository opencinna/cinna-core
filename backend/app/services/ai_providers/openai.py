"""OpenAI provider adapter, including the one key provisioner that exists.

WHAT A MINTED KEY CARRIES
-------------------------
A minted key carries write access to the project's API resources. Its blast
radius is bounded by the project's monthly spend limit, which Cinna verifies is
enforcing before minting into a project, and by revocation.

That is the whole of the claim, and it is deliberately not softened into
something more comforting. The provider documents default service-account
permissions as read and write of the project's API resources; nothing documents a
narrower guarantee we could inherit. We do not request key ``scopes`` in this
phase either — the vocabulary is an open string array with no enum, and no
artifact we hold contains it, so a guessed list would hard-fail the create call
the day the provider changed it. ``scopes_requested`` and ``scopes_granted`` are
recorded on every external ref regardless, so a later hardening pass changes two
values rather than rewriting the record.

The single enforcement lever is therefore the project spend limit. It is not
defence in depth, and it is checked *before* a key exists rather than applied
after one does.

ONE PROJECT
-----------
Single-project only. A project per user was considered and dropped: projects
cannot be deleted (archive only, and archive is irreversible), the ceiling is
about 2,000 per organisation, and archived projects are retained — so
project-per-user is a one-way ratchet that buys roughly 2,000 *lifetime*
provisions and two permanent failure classes no retry can resolve. Service
accounts have no documented ceiling, are genuinely deletable, and per-user cost
attribution is available under one project anyway.
"""
from __future__ import annotations

import logging

from app.models.credentials.ai_credential import AICredentialType
from app.services.ai_providers import _http
from app.services.ai_providers.base import (
    BaseProviderAdapter,
    MintedKey,
    ProbeResult,
    ProviderAdminError,
    ProvisionScope,
    SpendLimitStatus,
)

logger = logging.getLogger(__name__)

_MODELS_URL = "https://api.openai.com/v1/models"
_ADMIN_BASE = "https://api.openai.com/v1/organization"


def _list_models_blocking(api_key: str) -> list[str]:
    """List OpenAI models via GET /v1/models.

    ``httpx`` rather than the ``openai`` SDK, which is only present transitively
    today (via litellm) and is not a declared dependency.
    """
    payload = _http.get_json(_MODELS_URL, {"Authorization": f"Bearer {api_key}"})
    return _http.ids_from_openai_shape(payload)


class OpenAIKeyProvisioner:
    """Creates and destroys one service account per user, in one project.

    Every call goes over plain ``httpx``. The ``openai`` package is pinned at a
    version whose ``resources/`` contains no administration namespace at all, and
    is present only transitively (via litellm) — promoting a transitive pin
    across three major versions to reach four endpoints is the wrong trade, and
    it is the decision this tree already made and wrote down for the model
    listers.
    """

    # The provider has two create endpoints and they return the secret in two
    # different places: creating a service account nests it at
    # ``api_key.value``, while creating a key on an *existing* service account
    # returns a flat top-level ``value``. Only the first is used here. If the
    # second is ever added (for rotation), it needs its own parser — one that
    # "handles both" reads whichever field it finds and cannot tell a shape
    # change from a missing secret.

    def _headers(self, secret: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }

    async def _call(
        self,
        method: str,
        path: str,
        *,
        secret: str,
        json_body: dict | None = None,
        absent_on_404: bool = False,
    ) -> dict | list | None:
        """The one place this provisioner touches the network.

        Every administration call goes through here, which is what makes the rest
        of this class testable without a network and without patching a module
        attribute: a test subclasses it and overrides ``_call`` alone, so the
        spend-cap predicate, the null-secret rejection, the two distinct response
        parsers and the external-ref shape all still run for real. A stub that
        replaced ``mint`` instead would be testing the stub.
        """
        return await _http.admin_request(
            method,
            f"{_ADMIN_BASE}{path}",
            headers=self._headers(secret),
            json_body=json_body,
            absent_on_404=absent_on_404,
        )

    def _project_id(self, config: dict) -> str:
        project_id = (config or {}).get("project_id")
        if not project_id:
            raise ProviderAdminError("no_project_configured")
        return str(project_id)

    def config_schema(self) -> dict:
        """What an administrator must supply to connect an organisation."""
        return {
            "fields": [
                {
                    "name": "project_id",
                    "label": "Project ID",
                    "required": True,
                    "help": (
                        "The project minted keys are created in. Its monthly "
                        "spend limit is verified before the first key is minted."
                    ),
                },
                {
                    "name": "organization_id",
                    "label": "Organization ID",
                    "required": False,
                },
                {
                    "name": "spend_limit_cents",
                    "label": "Monthly spend limit (cents)",
                    "required": True,
                    "type": "integer",
                },
            ]
        }

    async def verify_admin_access(
        self, secret: str, config: dict
    ) -> str:
        """Read-only administration call. Returns the project id it reached."""
        project_id = self._project_id(config)
        payload = await self._call(
            "GET", f"/projects/{project_id}", secret=secret
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ProviderAdminError("provider_error", "unexpected project shape")
        return str(payload["id"])

    async def verify_spend_limit(
        self, secret: str, config: dict
    ) -> SpendLimitStatus:
        """Read the project's spend limit.

        A project with no limit comes back as ``absent`` — an explicit state, not
        a ``None`` for a caller to interpret. Whether any of these counts as a cap
        is :attr:`SpendLimitStatus.is_capped`'s business, not this method's.
        """
        project_id = self._project_id(config)
        payload = await self._call(
            "GET",
            f"/projects/{project_id}/spend_limit",
            secret=secret,
            absent_on_404=True,
        )
        if not isinstance(payload, dict):
            return SpendLimitStatus(enforcement_status="absent")
        enforcement = payload.get("enforcement")
        status = "inactive"
        if isinstance(enforcement, dict) and enforcement.get("status"):
            status = str(enforcement["status"])
        threshold = payload.get("threshold_amount")
        return SpendLimitStatus(
            enforcement_status=status,
            threshold_cents=int(threshold) if threshold is not None else None,
            currency=payload.get("currency"),
            interval=payload.get("interval"),
        )

    async def ensure_spend_limit(
        self, secret: str, config: dict
    ) -> SpendLimitStatus:
        """Set the configured limit if the project is not already capped.

        Called at **setup**, before any key exists. Never after a mint: capping
        a project that already has live keys in it leaves a window in which an
        uncapped key is in the world, and under one project that window need not
        exist at all.
        """
        current = await self.verify_spend_limit(secret, config)
        if current.is_capped:
            return current
        project_id = self._project_id(config)
        threshold = (config or {}).get("spend_limit_cents")
        if not threshold:
            raise ProviderAdminError("no_spend_limit_configured")
        await self._call(
            "POST",
            f"/projects/{project_id}/spend_limit",
            secret=secret,
            # Integer cents, and "month" is the only interval the provider
            # supports. Enforcement is documented as not instantaneous: recorded
            # spend can slightly exceed the cap, so nothing here promises a hard
            # stop.
            json_body={
                "threshold_amount": int(threshold),
                "currency": "USD",
                "interval": "month",
            },
        )
        return await self.verify_spend_limit(secret, config)

    async def mint(
        self, secret: str, *, label: str, scope: ProvisionScope
    ) -> MintedKey:
        """Create one service account and return its key.

        **Refuses to mint into an uncapped project**, checked here rather than at
        the call site so there is one implementation of the precondition and no
        second opinion about what "capped" means.
        """
        config = scope.config or {}
        project_id = self._project_id(config)

        limit = await self.verify_spend_limit(secret, config)
        if not limit.is_capped:
            raise ProviderAdminError("project_not_capped")

        payload = await self._call(
            "POST",
            f"/projects/{project_id}/service_accounts",
            secret=secret,
            # ``create_service_account_only`` is deliberately NOT sent: with it,
            # the response's ``api_key`` is null and there is no secret to
            # return. No ``scopes`` either — see the module docstring.
            json_body={"name": label},
        )
        if not isinstance(payload, dict):
            raise ProviderAdminError("provider_error", "unexpected create shape")

        service_account_id = payload.get("id")
        api_key = payload.get("api_key")
        # ``api_key`` is nullable in the provider's own schema. A null here is a
        # hard failure, never an empty key: an empty key stored on a child row is
        # indistinguishable from a real one until the moment somebody uses it.
        if not isinstance(api_key, dict) or not api_key.get("value"):
            raise ProviderAdminError("no_secret_returned")
        # **A missing id is representable, never an empty string.** It used to be
        # ``str(service_account_id or "")``, which put a falsy placeholder where
        # a handle belongs and made an absent id indistinguishable from a
        # provider that returned one. ``api_key_id`` immediately below has
        # always been nullable for exactly this reason; the two now agree.
        #
        # Not a raise, deliberately, and this is the one case where refusing the
        # response is the worse option. The service account already exists at the
        # provider by the time this line runs; raising discards the only two
        # handles we will ever have for it (``project_id`` and ``api_key_id``)
        # and leaves the key live with nothing but the provider's own console
        # naming it. Recording ``None`` keeps the mint honest in both directions:
        # the member gets the working key they were promised, ``revoke`` fails
        # loudly with a ``revoke_failed`` event carrying what handles there are,
        # and the gap is logged here rather than being discovered at revoke time.
        if not service_account_id:
            logger.error(
                "OpenAI create-service-account returned no id for project %s; "
                "the minted key cannot be revoked through the administration "
                "API and must be destroyed by hand.", project_id,
            )

        return MintedKey(
            api_key=str(api_key["value"]),
            external_ref={
                "project_id": project_id,
                "service_account_id": (
                    str(service_account_id) if service_account_id else None
                ),
                "api_key_id": str(api_key.get("id") or "") or None,
                # The shape stays true whether or not a later pass starts
                # scoping, so no child ever claims a narrowness it does not have.
                "scopes_requested": [],
                "scopes_granted": None,
            },
        )

    async def revoke(self, secret: str, external_ref: dict) -> None:
        """Destroy the service account, and with it the key.

        A 404 is success: the thing we were asked to destroy is gone. Treating it
        as a failure would make an already-revoked key retry forever.
        """
        ref = external_ref or {}
        project_id = ref.get("project_id")
        service_account_id = ref.get("service_account_id")
        if not project_id or not service_account_id:
            raise ProviderAdminError("incomplete_external_ref")
        await self._call(
            "DELETE",
            f"/projects/{project_id}/service_accounts/{service_account_id}",
            secret=secret,
            absent_on_404=True,
        )


class OpenAIAdapter(BaseProviderAdapter):
    type = AICredentialType.OPENAI
    label = "OpenAI"
    account_config_display_name = "OpenAI"
    account_config_slug = "openai"
    sdk_engine = "opencode/openai"
    bag_key_api_key = "openai_api_key"

    # OpenAI is the one provider whose administration API can create a key.
    key_provisioner = OpenAIKeyProvisioner()

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult:
        return await _http.run_listing(_list_models_blocking, api_key)
