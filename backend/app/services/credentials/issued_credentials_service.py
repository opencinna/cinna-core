"""
Issuer's cross-agent view of the access tokens they issued for their agents.

Backs ``GET /credentials/issued``. Each token product builds its own rows in its
own domain service (one batched query set per kind); this module only picks the
kinds, merges and orders. Revocation stays per product, on its own route.
"""
import uuid

from sqlmodel import Session

from app.models.credentials.issued_credential import (
    ISSUED_KIND_ORDER,
    IssuedCredentialKind,
    IssuedCredentialPublic,
    IssuedCredentialsPublic,
)
from app.services.a2a.access_token_service import AccessTokenService
from app.services.acp.connector_service import ACPConnectorService
from app.services.agent_api.agent_api_key_service import AgentApiKeyService
from app.utils import as_utc

_LISTERS = {
    IssuedCredentialKind.AGENT_API_KEY: AgentApiKeyService.list_issued_to_others,
    IssuedCredentialKind.A2A_TOKEN: AccessTokenService.list_issued_by,
    IssuedCredentialKind.ACP_TOKEN: ACPConnectorService.list_issued_by,
}


class IssuedCredentialsService:
    @staticmethod
    def list_issued(
        session: Session,
        issuer_id: uuid.UUID,
        include_ended: bool = False,
        filter_workspace: bool = False,
        workspace_id: uuid.UUID | None = None,
        kinds: list[IssuedCredentialKind] | None = None,
    ) -> IssuedCredentialsPublic:
        """Every token ``issuer_id`` issued, across agents and products.

        ``kinds`` restricts the products (None / empty = all). Order: agent name
        (removed agents last), kind, holder email (no holder last), newest first.
        """
        wanted = set(kinds) if kinds else set(_LISTERS)
        rows: list[IssuedCredentialPublic] = []
        for kind, lister in _LISTERS.items():
            if kind in wanted:
                rows.extend(
                    lister(
                        session,
                        issuer_id,
                        include_ended=include_ended,
                        filter_workspace=filter_workspace,
                        workspace_id=workspace_id,
                    )
                )

        # Newest first, then a stable sort on the grouping key keeps that order
        # inside each (agent, kind, holder) group.
        rows.sort(key=lambda row: as_utc(row.created_at), reverse=True)
        rows.sort(
            key=lambda row: (
                row.agent is None,
                (row.agent.name if row.agent else "").lower(),
                ISSUED_KIND_ORDER[row.kind.value],
                row.holder is None,
                ((row.holder.email if row.holder else None) or "").lower(),
            )
        )
        return IssuedCredentialsPublic(data=rows, count=len(rows))
