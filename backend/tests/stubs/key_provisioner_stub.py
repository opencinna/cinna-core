"""Recording key-provisioner stub — the provider's HTTP replaced, nothing else.

WHY IT SUBCLASSES THE REAL PROVISIONER
--------------------------------------
The interesting logic in a provisioner is not the HTTP; it is everything wrapped
around it. The refusal to mint into a project whose spend limit exists but is not
being enforced. The rejection of a create response whose ``api_key`` is null,
rather than storing an empty key. The exact shape of the external ref, including
the ``scopes_requested`` / ``scopes_granted`` fields a later hardening pass will
fill in. A stub that replaced ``mint`` wholesale would test none of that — it
would test the stub, and every one of those rules would be free to rot.

So this overrides exactly one method: ``OpenAIKeyProvisioner._call``, the single
seam through which the real class reaches the network. Everything above it runs
for real.

It is also **not** a ``unittest.mock.patch`` on a module attribute. The stub is
installed through the registry override, so any call site that resolves an
adapter is intercepted no matter which module makes the call — and calls are
counted, so "the stub was never reached" fails loudly instead of passing quietly
while the suite talks to the real provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.ai_providers.base import ProviderAdminError
from app.services.ai_providers.openai import OpenAIKeyProvisioner


@dataclass
class AdminCall:
    """One recorded administration request."""

    method: str
    path: str
    json_body: dict | None = None


@dataclass
class ProvisionerRecorder:
    """The call log, plus the assertions worth having on it."""

    calls: list[AdminCall] = field(default_factory=list)
    #: Service accounts the stub says it created, in order.
    minted: list[str] = field(default_factory=list)
    #: Service account ids the stub was asked to destroy, in order.
    revoked: list[str] = field(default_factory=list)

    @property
    def mint_count(self) -> int:
        return len(self.minted)

    @property
    def revoke_count(self) -> int:
        return len(self.revoked)

    def assert_minted(self, times: int = 1) -> None:
        assert self.mint_count == times, (
            f"expected {times} mint(s), got {self.mint_count}. A count of 0 means "
            f"the call went around the registry — i.e. at the real provider."
        )

    def assert_revoked(self, *service_account_ids: str) -> None:
        assert self.revoked == list(service_account_ids), (
            f"expected revocations of {list(service_account_ids)}, got "
            f"{self.revoked}"
        )


class StubOpenAIProvisioner(OpenAIKeyProvisioner):
    """The real OpenAI provisioner with a canned provider behind it.

    Everything is configured as *provider behaviour*, never as a short-circuit of
    our own logic — ``spend_limit_absent=True`` makes the provider report a
    project with no hard limit at all and lets the real refusal fire, rather than
    making the stub refuse.

    **The default is ``inactive`` because that is what a correctly capped project
    returns.** ``enforcement.status`` is the limit's *current* runtime state, not
    its configuration: a project capped at $100 and sitting at $0 reports
    ``inactive``, and reports ``enforcing`` only once it has hit the threshold and
    is already refusing traffic. This default used to be ``enforcing``, which
    described a project that was out of money and made the stub agree with a
    predicate that could never pass against the real provider.
    """

    def __init__(
        self,
        recorder: ProvisionerRecorder,
        *,
        enforcement_status: str = "inactive",
        threshold_cents: int | None = 5000,
        spend_limit_absent: bool = False,
        mint_error: str | None = None,
        revoke_error: str | None = None,
        omit_secret: bool = False,
        project_missing: bool = False,
        on_mint=None,
    ) -> None:
        self.recorder = recorder
        self.enforcement_status = enforcement_status
        self.threshold_cents = threshold_cents
        self.spend_limit_absent = spend_limit_absent
        self.mint_error = mint_error
        self.revoke_error = revoke_error
        self.omit_secret = omit_secret
        self.project_missing = project_missing
        #: Called once, from inside the create call and before the provider
        #: decides whether it succeeds, so it reaches the failure path as well
        #: as the success one. This is the only way to reproduce a *concurrent*
        #: action landing inside the provider await — the window in which the
        #: key exists and no row names it — without two sessions racing a real
        #: network call. What the callback does is the test's business and is
        #: expected to be a real API request.
        self.on_mint = on_mint

    async def _call(
        self,
        method: str,
        path: str,
        *,
        secret: str,
        json_body: dict | None = None,
        absent_on_404: bool = False,
    ):
        self.recorder.calls.append(AdminCall(method, path, json_body))

        if path.endswith("/spend_limit"):
            # **Read-only, matching the code under test.** The limit is set on the
            # provider's console and Cinna has no way to write one, so this stub
            # answers a GET and nothing else. A write arriving here is a genuine
            # regression and falls through to the unhandled-path assertion at the
            # bottom rather than being quietly served.
            if method == "GET":
                if self.spend_limit_absent:
                    # What ``absent_on_404`` produces: no limit row at all.
                    return None
                return {
                    "threshold_amount": self.threshold_cents,
                    "currency": "USD",
                    "interval": "month",
                    "enforcement": {"status": self.enforcement_status},
                }

        if path.endswith("/service_accounts") and method == "POST":
            if self.on_mint is not None:
                # Fires exactly once — a second mint in the same test is a
                # retry, and re-running the concurrent action would race a
                # window the test has already closed.
                #
                # **Before the provider decides whether it succeeds**, so the
                # hook reaches the failure path too. It used to fire after the
                # key was created, which meant a concurrent action could only
                # ever land inside a *successful* mint — and the write the
                # failure path makes (``pending`` with a retry time, which
                # resurrects a row somebody else settled) was unreachable from
                # the suite as a result.
                hook, self.on_mint = self.on_mint, None
                hook()
            if self.mint_error:
                raise ProviderAdminError(self.mint_error)
            index = len(self.recorder.minted) + 1
            service_account_id = f"svc_{index}"
            self.recorder.minted.append(service_account_id)
            return {
                "id": service_account_id,
                "name": (json_body or {}).get("name"),
                # Nullable in the provider's own schema. ``omit_secret``
                # reproduces that, and the real code must treat it as a hard
                # failure rather than as an empty key.
                "api_key": (
                    None
                    if self.omit_secret
                    else {
                        "id": f"key_{index}",
                        "value": f"sk-proj-minted-{index}",
                    }
                ),
            }

        if method == "DELETE" and "/service_accounts/" in path:
            if self.revoke_error:
                raise ProviderAdminError(self.revoke_error)
            self.recorder.revoked.append(path.rsplit("/", 1)[-1])
            return None

        if method == "GET":
            if self.project_missing:
                raise ProviderAdminError("project_not_found")
            return {"id": path.rsplit("/", 1)[-1], "object": "organization.project"}

        raise AssertionError(f"unexpected administration call: {method} {path}")
