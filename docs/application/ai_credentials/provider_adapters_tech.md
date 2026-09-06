# AI Provider Adapters — Technical Reference

The registry that answers every "what does this provider do" question in one place.

> This is a **tech-only** document: the adapter package has no user-facing behaviour of its own. Its consumers are documented in [ai_credentials](ai_credentials.md) / [ai_credentials_tech](ai_credentials_tech.md) and [admin_ai_credential_provisioning](admin_ai_credential_provisioning.md) / [_tech](admin_ai_credential_provisioning_tech.md).

---

## Files

| Path | Contents |
|------|----------|
| `backend/app/services/ai_providers/__init__.py` | Package facade — re-exports the contract types and the registry functions |
| `backend/app/services/ai_providers/base.py` | The `AIProviderAdapter` Protocol, `BaseProviderAdapter` (shared implementation), `ProbeResult`, the probe reason codes, `KeyClassification`, and the optional-capability types `KeyProvisioner` / `MintedKey` / `ProvisionScope` / `SpendLimitStatus` / `ProviderAdminError` |
| `backend/app/services/ai_providers/registry.py` | `_ADAPTERS` (the one dict keyed on `AICredentialType`), `find_adapter` / `get_adapter` / `all_adapters` / `supported_types`, `UnknownProviderError`, and the `override_for_tests` seam |
| `backend/app/services/ai_providers/_http.py` | Shared blocking-HTTP helpers: `get_json`, `ids_from_openai_shape`, `ok_result`, `map_auth_error`, `run_listing` (probes) and `_admin_error`, `_request_blocking`, `admin_request` (administration calls) |
| `backend/app/services/ai_providers/anthropic.py` | `AnthropicAdapter` — the `sk-ant-oat` / `sk-ant-api` classification rule |
| `backend/app/services/ai_providers/openai.py` | `OpenAIAdapter` **and** `OpenAIKeyProvisioner` — the only provisioner that exists |
| `backend/app/services/ai_providers/google.py` | `GoogleAdapter` — the one adapter that lists through a vendor SDK (`google-genai`) |
| `backend/app/services/ai_providers/minimax.py` | `MiniMaxAdapter` — catalog-only, no model-list endpoint |
| `backend/app/services/ai_providers/openai_compatible.py` | `OpenAICompatibleAdapter` — a shape rather than a vendor |
| `backend/tests/architecture/provider_adapter_registry_test.py` | The executable form of the package's property (below), plus contract-completeness checks over every registered adapter |
| `backend/tests/unit/ai_provider_adapters_test.py` | Per-adapter unit tests (31) |
| `backend/tests/utils/ai_provider.py` | `stub_all_providers` / `stub_minting_providers` and the `probe_*` result builders |
| `backend/tests/stubs/provider_adapter_stub.py`, `backend/tests/stubs/key_provisioner_stub.py` | The recording stubs installed through `override_for_tests` |

---

## The property this package enforces

> **No dictionary keyed on `AICredentialType` members may be declared outside `app/services/ai_providers/`.**

`ALLOWED_PROVIDER_TABLE_MODULES` in the architecture test is **an empty set and is meant to stay empty**. If a table is genuinely provider-shaped it belongs on an adapter; if it is not provider-shaped it is not keyed on `AICredentialType`.

A scattered `cred.type == AICredentialType.ANTHROPIC` inside a feature is deliberately **not** forbidden — that is provider-specific *logic*, which lives with its feature. A per-provider *table* is what forces an edit in N files when a provider is added, and that is what the test catches. (The alternative property — forbid the comparisons — was measured at 26 comparisons across nine modules, would have needed a six-file allowlist, and would still have missed five hardcoded provider constants passed as *arguments* in one route handler.)

### The known hole: tables keyed on the enum's string values

`{"anthropic": …, "openai": …}` is a per-provider table and no AST check can tell it from any other string-keyed dict. **Two such tables exist today**, both in `environment_lifecycle._write_opencode_config` (`backend/app/services/environments/environment_lifecycle.py`, around lines 2601 and 2685):

| Table | Shape | Assessment |
|-------|-------|-----------|
| provider name → OpenCode provider id (`"openai_compatible"` → `"custom"`) | genuinely provider-shaped | **Belongs on the adapter one day.** Not absorbed in this pass |
| provider name → the already-extracted local API-key variable | dispatch over local variables | Not provider-shaped in the sense the property means; it does not belong on an adapter |

Neither is covered, deliberately: widening the predicate to catch string-keyed dicts would mean guessing, and the guesses would have to be waved through by an allowlist — and an **empty allowlist is the entire basis of the test's design**. This is recorded rather than silently tolerated. Green means no per-provider table keyed on the enum *members* is declared outside the package.

### What else the test asserts

`test_registry_serves_every_credential_type`, `test_registry_order_follows_the_enum`, `test_every_adapter_declares_the_full_contract`, `test_adapter_types_are_unique_and_match_their_registry_slot`, `test_a_declared_provisioner_implements_the_whole_contract`, `test_anthropic_never_declares_a_key_provisioner`, `test_provisioner_config_fields_fit_the_stored_config_model`, plus three tests that exercise the AST scan itself against a planted fixture tree (so a scan that silently stopped walking would fail).

---

## The adapter contract

`AIProviderAdapter` is a `typing.Protocol`, not a base class — a test can substitute a plain object through `override_for_tests` without inheriting anything. The five shipped adapters extend `BaseProviderAdapter`, which supplies the defaults.

### Declared data

| Attribute | Meaning |
|-----------|---------|
| `type` | The `AICredentialType` this adapter serves |
| `label` | Human name. The single answer to "what is this provider called" |
| `account_config_display_name` | What `GET /external/account-config` calls it. **The empty string is a deliberate sentinel** meaning "use the credential's own free-form name" — `openai_compatible` only |
| `account_config_slug` | Stable slug the native client keys its provider descriptor on |
| `sdk_engine` | `<engine>/<provider>` for the AddEnvironment SDK picker. The single declaration of that mapping |
| `bag_key_api_key` / `bag_key_base_url` / `bag_key_model` | Credential-bag slot names; the latter two are `None` for providers that carry neither |
| `requires_base_url` / `requires_model` | The server-side "which fields does this provider require" answer — the authority behind the 400s the create/update routes raise |
| `supports_model_listing` | Whether the provider publishes a model-list endpoint at all |
| `issues_oauth_tokens` | Whether `classify_key` can ever answer `is_oauth_token=True`. Exists for the callers that must **decrypt** a key in order to classify it: for an API-key-only provider the answer is known in advance and the decrypt is pure cost |
| `key_provisioner` | `KeyProvisioner \| None`. Non-`None` on `OpenAIAdapter` alone — but see [Two kinds of `None`](#two-kinds-of-none) before reading the other four as one group |

### Derived properties

| Property | Derivation |
|----------|-----------|
| `bag_keys` | The declared slots, in declaration order |
| `catalog_engine_provider` | `sdk_engine.partition("/")` → `(engine, provider)`. Derived rather than declared: these were two separate five-entry tables in two modules holding the same mapping in two encodings |
| `supports_minting` | `key_provisioner is not None` |

### Behaviour

| Method | Contract |
|--------|----------|
| `classify_key(api_key) -> KeyClassification` | `is_oauth_token`, `env_var_name`, `label`. Every provider answers; API-key-only providers always answer `is_oauth_token=False`, which is what lets a caller ask **without** first checking `cred.type == ANTHROPIC`. `env_var_name` is non-`None` only for Anthropic (`ANTHROPIC_API_KEY` vs `CLAUDE_CODE_OAUTH_TOKEN`) |
| `apply_to_bag(bag, data)` | Writes decrypted credential data into the environment credential bag. Touches only the slots this adapter declares, so a provider cannot clobber another's |
| `list_models(api_key, base_url=None) -> ProbeResult` | Pure I/O. No DB access, never logs the key, blocking HTTP via `anyio.to_thread` |

### Import rule

Nothing in this package may import a service that imports it back — models, stdlib and HTTP clients only. `ProbeResult` and the reason codes live in `base.py` rather than in `model_discovery_service` for exactly that reason (that module imports `ai_credentials_service`, and an adapter reaching back would close the cycle). `model_discovery_service` **re-exports** them so existing import paths keep working.

---

## Probe results and reason codes

| Constant | Meaning |
|----------|---------|
| `OAUTH_TOKEN_UNSUPPORTED` | An Anthropic OAuth token — cannot call the models endpoint, but the credential is fine |
| `NO_LIST_ENDPOINT` | The provider publishes no model list (catalog-only) |
| `NO_BASE_URL` | An OpenAI-compatible credential with no base URL — nowhere to ask |
| `UNSUPPORTED_TYPE` | The credential's `type` resolves to no adapter |
| `SKIP_REASONS` | `frozenset` of the four above |
| `ERROR_INVALID_KEY` (`"invalid_key"`) | A real auth rejection |

`ProbeResult.ok` is `True` for both a successful listing **and** a benign skip; `False` only on `invalid_key`. `ProbeResult.is_skip` is `ok and reason in SKIP_REASONS`.

### Known gap — a vendor SDK's own error bypasses the `invalid_key` mapping

`_http.run_listing` maps **only** `httpx.HTTPStatusError` to `invalid_key`. `google-genai` raises `google.genai.errors.ClientError`, which is not an `httpx` exception, so a rejected Google key propagates instead of mapping.

**Index by symptom:**

- **A 500 from Test Connection** when the Google key is bad, instead of the normal "invalid key" result.
- **`"ClientError"` recorded as the failure reason in the model-discovery cron**, instead of `invalid_key`.

**Pre-existing, not introduced by the adapter package** — the single `except` around the old dispatch had the identical hole — and preserved deliberately: fixing it is a behaviour change that wants its own test. Stated at the mapping site (`_http.run_listing`'s docstring) and repeated on `GoogleAdapter.list_models`.

### What `ids_from_openai_shape` tolerates, and what it refuses

**A payload that is neither an object nor an array raises** (`TypeError`), exactly as the per-provider listers it replaced did. An earlier draft of this package let that case fall through to an empty list, and the docstring described the tolerance without the consequence chain. The chain is the reason the raise is back:

1. An empty list is not a neutral value here — it is the **success** branch.
2. `discover_models_for_credential` takes that branch by overwriting `discovered_models` with `[]` **and clearing `models_discovery_error`** (the `_http` docstring calls it `refresh_credential_models`, which is not the name it has). A garbled response therefore wipes a good cached model list *and* erases the record that anything went wrong.
3. `model_health` then reads the wiped cache as "empty but present", skips both its discovery branch and its unverified guard, and reports **OK** — so a broken provider response silently flips health from `UNVERIFIED` to `OK`.

A raise instead records the exception class name against the credential and leaves the previous list alone, which is what every other malformed-response case in this module does.

The tolerance that *is* deliberate is exactly two things:

- A **bare array** is accepted where the Anthropic and OpenAI listers would have raised. (The openai-compatible lister *looked* like it already handled one — `payload.get("data", payload if isinstance(payload, list) else [])` — but `.get` on a list raises before the default is evaluated, so that branch was dead.)
- Individual **entries** that are not dicts, or carry no `id`, are skipped rather than raising. This one does yield a short list on the success branch, so a response with some unusable entries caches the usable ones — bounded by there being a well-formed envelope around them.

---

## Registry and the test seam

```python
from app.services.ai_providers import registry

registry.find_adapter(cred_type)   # -> AIProviderAdapter | None   (unknown type is a normal outcome)
registry.get_adapter(cred_type)    # -> AIProviderAdapter          (raises UnknownProviderError; unknown type is a bug)
registry.all_adapters()            # -> list, in enum declaration order, overrides applied
registry.supported_types()         # -> list[AICredentialType], in enum declaration order
```

`_coerce` accepts the enum, a plain string, or `None`, because rows load their `type` as either the enum or a string depending on the path and callers pass raw request values.

### `override_for_tests`

```python
with registry.override_for_tests(stub):            # replaces the entry for stub.type
with registry.override_for_tests(for_all=stub):    # replaces every entry with one object
```

**The test seam is in the registry deliberately.** A `unittest.mock.patch` on a module attribute stops intercepting the moment a call site is rewritten to reach the provider by another route — and it does so *silently*: the patch target still resolves, the test still passes, and the suite starts making real HTTPS calls. Overriding what the registry *returns* intercepts any call that goes through the registry, whichever module makes it; a call that bypasses the registry fails loudly against a stub that counts its invocations.

- `_overrides` is a **module-level dict, not a `ContextVar`** — the test client runs the app on a different thread from the test function, and a `ContextVar` set in the test would not be visible there.
- Restores the previous mapping on exit including on exception, so a failing test cannot leak a stub into the next one.
- Raises `UnknownProviderError` for an adapter whose `type` resolves to no provider: such an override could never be looked up, so it would silently be a no-op and the test would reach the real provider.
- Prefer one stub per provider (`tests/utils/ai_provider.stub_all_providers` builds them by wrapping the real adapters). `for_all` hands the same object to every type, which also replaces every per-provider *fact* — bag slots, SDK engine, display name — and reshapes anything reading them.

---

## The five adapters

| Type | `label` | `account_config_display_name` / `slug` | `sdk_engine` | bag key | Lists models | OAuth | Mints |
|------|---------|--------------------------------------|--------------|---------|--------------|-------|-------|
| `ANTHROPIC` | Anthropic | Anthropic / `anthropic` | `claude-code/anthropic` | `anthropic_api_key` | yes | **yes** | no |
| `MINIMAX` | MiniMax | MiniMax / `minimax` | `claude-code/minimax` | `minimax_api_key` | no (`NO_LIST_ENDPOINT`) | no | no |
| `OPENAI_COMPATIBLE` | OpenAI-Compatible | `""` (sentinel) / `openai_compatible` | `opencode/openai_compatible` | `openai_compatible_api_key` (+ base_url, model) | yes, needs base URL | no | no |
| `OPENAI` | OpenAI | OpenAI / `openai` | `opencode/openai` | `openai_api_key` | yes | no | **yes** |
| `GOOGLE` | Google | **Gemini** / `gemini` | `opencode/google` | `google_api_key` | yes (via `google-genai`) | no | no |

### Two kinds of `None`

The `Mints` column has one `yes` and four `no`s, and the four are **not the same fact**. The distinction is stated on `key_provisioner` in `base.py` and is repeated here because a reader who groups them will write the wrong sentence:

- **Anthropic's `None` is permanent, and it is a fact about the provider rather than about us.** Its administration API can list and update API keys and cannot create them — in the API and in every SDK. No amount of work on this side changes that. Which is why an Anthropic key added by hand is a **first-class path**, not a fallback or a degraded mode, and why nothing here should ever be written that reads as "minting is coming for Anthropic".
- **MiniMax, Google and `openai_compatible` are `None` because nobody has built one.** That is an absence of work, not a provider limitation, and a future pass may fill any of them in.

`supports_minting` is the predicate every caller asks; nothing reads `key_provisioner` itself to decide anything.

Two further notes worth keeping:

- **Google's display name is `Gemini` on purpose.** The native/desktop bundle has always called this provider "Gemini"; the browser calls it "Google" in one place and "Google AI" in two others. Reconciling those three is a follow-up, and it must not be done by quietly changing what the desktop client receives.
- **`httpx` rather than a vendor SDK** is a decision this tree already made and wrote down: the `openai` package is present only transitively (via litellm) and `anthropic` is not a dependency at all. Google is the exception because its listing has no documented REST shape we rely on.

---

## The optional minting capability

`KeyProvisioner` is a `Protocol` an adapter may declare on `key_provisioner`. Every method raises `ProviderAdminError` for a provider-side failure, so callers branch on a stable code rather than on an HTTP status they would have to re-interpret.

| Method | Purpose |
|--------|---------|
| `mint(secret, *, label, scope) -> MintedKey` | Create one key. **Refuses to mint into an uncapped project.** Returns the key plus the handles needed to revoke it |
| `revoke(secret, external_ref) -> None` | Destroy the key. A 404 is success — the thing we were asked to destroy is gone; treating it as a failure would make an already-revoked key retry forever |
| `verify_admin_access(secret, config) -> str` | Read-only administration call. Returns the provider-side id it reached |
| `verify_spend_limit(secret, config) -> SpendLimitStatus` | Read the project's cap |
| `ensure_spend_limit(secret, config) -> SpendLimitStatus` | Set the configured cap if the project is not already capped. **Setup only**, never after a mint |
| `config_schema() -> dict` | What an administrator must supply to connect an organisation. Published verbatim as `admin_config_schema` by `GET /admin/provider-adapters/` |

### `SpendLimitStatus.is_capped` is the predicate, defined once

```python
@property
def is_capped(self) -> bool:
    return self.enforcement_status == "enforcing"
```

`enforcement_status` is one of `enforcing` / `inactive` / `absent` — an explicit value in every case, including "the project has no limit at all". A limit that exists but reports `inactive` **is not a cap**, and reading `threshold_amount` alone (the obvious field) would call it one. Every caller asks this object; none re-derives the rule. `threshold_cents` is **integer cents**, with the unit in the name at every layer because an off-by-100 here is a hundred-fold cap.

### `ProviderAdminError` codes

Raised by `_http._admin_error` and by the provisioner itself. The provider's own response body is **never** stored — it is unbounded, occasionally echoes request material, and a reason code is what an operator can act on.

| Code | Source |
|------|--------|
| `invalid_admin_secret` | HTTP 401 / 403 |
| `project_not_found` | HTTP 404 |
| `project_spend_limit_exceeded` / `organization_spend_limit_exceeded` | HTTP 429 with the matching `error.code` |
| `rate_limited` | HTTP 429 otherwise |
| `provider_error` | any other HTTP status, or an unexpected response shape |
| `provider_unreachable` | an `httpx` transport failure (DNS, connect, read timeout) |
| `no_project_configured` / `no_spend_limit_configured` | the configuration is incomplete |
| `no_secret_returned` | the create response's `api_key` was null |
| `incomplete_external_ref` | a revoke was asked for with handles it cannot use |
| `project_not_capped` | `mint` refused because `is_capped` was False |

**The 429 split is deliberate.** Two entirely different conditions share that status: an ordinary rate limit (retry later, nothing is wrong) and a spend cap that has been reached (retrying will never help). The provider distinguishes them in `error.code`; collapsing them would produce a converge loop that burns its whole attempt budget against an exhausted account.

### `OpenAIKeyProvisioner` — endpoints and the one test seam

All calls go over plain `httpx` against `https://api.openai.com/v1/organization`. The pinned `openai` package contains **no administration namespace at all** and is present only transitively; promoting a transitive pin across three major versions to reach four endpoints is the wrong trade.

| Call | Endpoint |
|------|----------|
| `verify_admin_access` | `GET /projects/{project_id}` |
| `verify_spend_limit` | `GET /projects/{project_id}/spend_limit` (`absent_on_404=True`) |
| `ensure_spend_limit` | `POST /projects/{project_id}/spend_limit` — `{threshold_amount: <int cents>, currency: "USD", interval: "month"}` |
| `mint` | `POST /projects/{project_id}/service_accounts` — `{name: label}` |
| `revoke` | `DELETE /projects/{project_id}/service_accounts/{service_account_id}` (`absent_on_404=True`) |

`_call` is **the one place this provisioner touches the network**, and that is what makes the class testable without a network and without patching a module attribute: `tests/stubs/key_provisioner_stub.py` subclasses it and overrides `_call` alone, so the spend-cap predicate, the null-secret rejection, the response parser and the external-ref shape all still run for real. A stub that replaced `mint` would be testing the stub.

Two details in `mint` that are load-bearing:

- **`create_service_account_only` is deliberately not sent.** With it, the response's `api_key` is null and there is no secret to return.
- **A null `api_key` is a hard failure (`no_secret_returned`), never an empty key.** An empty key stored on a child row is indistinguishable from a real one until the moment somebody uses it.

The provider has two create endpoints returning the secret in two different places — creating a service account nests it at `api_key.value`, creating a key on an *existing* service account returns a flat top-level `value`. Only the first is used. If the second is ever added (for rotation) it needs its **own** parser: one that "handles both" reads whichever field it finds and cannot tell a shape change from a missing secret.

### `MintedKey.external_ref` shape

```json
{
  "project_id": "proj_…",
  "service_account_id": "svc_acct_…",
  "api_key_id": "key_…",
  "scopes_requested": [],
  "scopes_granted": null
}
```

`scopes_requested` / `scopes_granted` are recorded on **every** external ref even though we do not send `scopes` today — see [What a minted key carries](admin_ai_credential_provisioning.md#what-a-minted-key-carries). The shape stays true whether or not a later pass starts scoping, so no child ever claims a narrowness it does not have, and hardening is a change to two values rather than a rewrite of the record.

---

## `GET /api/v1/admin/provider-adapters/`

Superuser only. `ProviderAdaptersPublic` → `{ "data": [ProviderAdapterPublic], "count": int }`.

Derived from `registry.all_adapters()`, so a sixth provider appears the moment its adapter is registered — no list to update, and no way for this endpoint to disagree with the validation it describes.

| Field | Source |
|-------|--------|
| `type`, `label`, `account_config_display_name`, `account_config_slug`, `sdk_engine` | adapter attributes, verbatim |
| `requires_base_url`, `requires_model`, `supports_model_listing`, `issues_oauth_tokens`, `supports_minting` | adapter attributes, verbatim |
| `admin_config_schema` | `adapter.key_provisioner.config_schema()`, or `None` when the adapter cannot mint |
| `can_mint_now` | **Required, no default.** `adapter.supports_minting and adapter.type in connected_types`, where `connected_types` is one `SELECT DISTINCT provider_type FROM provider_admin_credential` for the whole response |

`supports_minting` and `can_mint_now` are both published on purpose. The first is a fact about the provider and is what tells an admin *why* the option is unavailable; the second is the **policy answer**. `can_mint_now` has no default because an absent policy answer read through a client-side fallback is the browser deciding the policy — and the create route enforces the same rule in `ManagedAICredentialsService._validate_provisioning_shape`, so a conjunction rebuilt in the client is the copy that stops agreeing the day a third condition is added.

> **Envelope note.** This endpoint returns `{data, count}`; `GET /admin/provider-admin-credentials/` and `GET /ai-credentials/provisioning` return bare arrays. See [Known gaps](admin_ai_credential_provisioning.md#known-gaps).

---

## What the adapters absorbed

Half A of this phase moved existing per-provider tables onto the adapters rather than adding new ones. What used to be declared elsewhere and is now derived:

| Was | Now |
|-----|-----|
| `_TYPE_TO_SDK_ENGINE` in `managed_ai_credentials_service` **and** a byte-identical copy in a second module | `adapter.sdk_engine`, via `_sdk_engine_for()` |
| A third encoding of the same mapping split into `(engine, provider)` tuples in `external_account_config_service` | `adapter.catalog_engine_provider`, derived by splitting `sdk_engine` on `/` |
| `SDK_API_KEY_MAP` / `CREDENTIAL_TYPE_TO_BAG_KEY` / `make_empty_credential_bag` / `apply_credential_to_bag` declaring the provider axis a second time in `sdk_constants` | derived from the adapters; `sdk_constants` keeps only the **SDK-engine** axis (`SDK_TO_CREDENTIAL_TYPE`, `SDK_CREDENTIAL_COMPATIBILITY`, …) |
| Four per-provider model listers inside `model_discovery_service`'s dispatch | `adapter.list_models`, one `registry.find_adapter` lookup |
| The `sk-ant-oat` prefix test written out independently in six places | `adapter.classify_key`; `utils.detect_anthropic_credential_type` is a facade over it — see [anthropic_credential_types_tech](anthropic_credential_types_tech.md) |

---

## Integration points

- [ai_credentials_tech](ai_credentials_tech.md) — the probe path, `model_discovery_service`, `sdk_constants`, the credential bag
- [admin_ai_credential_provisioning](admin_ai_credential_provisioning.md) / [_tech](admin_ai_credential_provisioning_tech.md) — provider admin credentials, per-user key minting, and everything the `KeyProvisioner` capability feeds
- [anthropic_credential_types_tech](anthropic_credential_types_tech.md) — `classify_key` and the two Anthropic environment variables
- [Multi-SDK environments](../../agents/agent_environment_core/multi_sdk.md) — the SDK-engine axis `sdk_engine` feeds, and the credential bag the adapters fill
