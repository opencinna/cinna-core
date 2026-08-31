import secrets
import warnings
from typing import Annotated, Any, Literal

from pydantic import (
    AnyUrl,
    BeforeValidator,
    EmailStr,
    Field,
    HttpUrl,
    PostgresDsn,
    computed_field,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Self


def parse_cors(v: Any) -> list[str] | str:
    if isinstance(v, str) and not v.startswith("["):
        return [i.strip() for i in v.split(",") if i.strip()]
    elif isinstance(v, list | str):
        return v
    raise ValueError(v)


#: The one value of ``ROUTING_TRACE_RETENTION_DAYS`` that means "never expire
#: routing traces". Deliberately ``-1`` and not ``0``: an operator typing ``0``
#: into a retention window means "keep nothing", and a knob whose most natural
#: "off" value silently meant *unbounded* retention would invert the exposure
#: argument that permits storing message text at all (plan §4/§7). ``0`` is
#: therefore rejected outright rather than reinterpreted. Shared with
#: ``RoutingTraceService.purge`` so both ends agree on the sentinel.
ROUTING_TRACE_RETENTION_FOREVER = -1

#: The three values of ``ROUTING_TRACE_APP_MCP_MODE``. Shared constants rather
#: than literals because three modules branch on them — the producer
#: (``AppMCPRoutingService.route_message``), the write gate
#: (``RoutingTraceService.persist``) and the startup validator below — and a
#: typo in any one of them would degrade to "not off, not full", i.e. silently
#: to ``metadata``, which is the shape of failure this setting was removed over
#: the first time. See the setting for what each value stores and omits.
ROUTING_TRACE_APP_MCP_OFF = "off"
ROUTING_TRACE_APP_MCP_METADATA = "metadata"
ROUTING_TRACE_APP_MCP_FULL = "full"
ROUTING_TRACE_APP_MCP_MODES = (
    ROUTING_TRACE_APP_MCP_OFF,
    ROUTING_TRACE_APP_MCP_METADATA,
    ROUTING_TRACE_APP_MCP_FULL,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
    )
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = secrets.token_urlsafe(32)
    # Encryption key for sensitive credential fields (32 bytes for AES-256)
    ENCRYPTION_KEY: str = secrets.token_urlsafe(32)
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    FRONTEND_HOST: str = "http://localhost:5173"
    # Publicly reachable origin of THIS backend. Every absolute URL that points
    # at the API and is handed to something outside the platform is built from
    # it: inbound webhook URLs (server channels, task triggers, agent hooks),
    # the consumer-facing Agent REST API base, and signed A2A attachment
    # download links. It is deliberately separate from FRONTEND_HOST — in a
    # real deployment the SPA is on dashboard.example.com while the API answers
    # on api.example.com, and a URL pointing at the SPA origin 404s. For local
    # testing against an external provider, point it at the HTTPS tunnel in
    # front of the backend (see `make webhook-tunnel`).
    # Empty = fall back to FRONTEND_HOST, preserving the single-origin
    # behaviour deployments had before this setting existed.
    BACKEND_BASE_URL: str = ""
    # Former name, still honoured. It was introduced for the webhook URLs only,
    # then the same bug turned up on the agent-api and A2A file URLs, so the
    # setting outgrew its name. Deployments that already set it keep working;
    # BACKEND_BASE_URL wins when both are present.
    WEBHOOK_BASE_URL: str = ""
    MCP_SERVER_BASE_URL: str = ""
    # Internal/container-reachable MCP origin. The public MCP_SERVER_BASE_URL is
    # not always routable from inside the agent network, so for agent2agent
    # providers the env-synced manifest copy of the endpoint URL has its netloc
    # rewritten to this origin (RD-4). The stored credential + UI keep the public
    # URL. When empty, no rewrite happens (single-host/dev deployments). Used by
    # the Phase 4 manifest collector.
    MCP_SERVER_CONTAINER_URL: str = ""
    # SSRF/egress guard (RD-6): backend-initiated calls to external MCP servers
    # (DCR registration, OAuth refresh, connectivity probe) reject internal /
    # link-local / private ranges unless this is True. Default false; a
    # self-hosted operator may flip it to reach private MCP servers.
    MCP_PROVIDER_ALLOW_PRIVATE_HOSTS: bool = False
    # SSRF/egress guard for git-source operations: backend-initiated git
    # network calls (clone / pull / push / ls-remote) reject internal /
    # link-local / private ranges unless this is True. Independent of the MCP
    # setting so a self-hosted operator can host git on a private LAN without
    # also opening up MCP egress. Default false.
    GIT_SOURCE_ALLOW_PRIVATE_HOSTS: bool = False
    # Per-file size cap (bytes) for files captured under a git source's
    # ``workspace/`` subtree. Enforced on checkout AND pull (inbound) before the
    # tree is seeded, and on push (outbound) before the commit — binary-in-git
    # hygiene + a guard against a malicious repo shipping a huge file. Default
    # 10 MiB.
    GIT_SOURCE_MAX_FILE_BYTES: int = 10 * 1024 * 1024
    # Bounded network timeout (seconds) for backend-initiated git remote calls
    # (clone / ls-remote / fetch / log / push). A hung or slow remote must fail
    # fast rather than pin a worker (and, for the status reads, a pooled DB
    # connection). Applied as the GitPython ``kill_after_timeout`` hard stop on
    # the clone / ls-remote subprocesses, the HTTP low-speed-abort window
    # (``GIT_HTTP_LOW_SPEED_LIMIT`` / ``GIT_HTTP_LOW_SPEED_TIME``), and the SSH
    # ``ConnectTimeout`` / keepalive. Default 30s.
    GIT_SOURCE_NETWORK_TIMEOUT_SECONDS: int = 30
    # Redirect URI for the MCP-provider OAuth/DCR authorization-code flow
    # (Phase 5). The target AS redirects the browser back here after consent; the
    # frontend route forwards (code, state) to POST /mcp-providers/oauth/callback.
    # Must be registered with the target AS during DCR. Defaults to a frontend
    # route mirroring the Google credential OAuth callback.
    MCP_PROVIDER_OAUTH_REDIRECT_URI: str = (
        "http://localhost:5173/mcp-providers/oauth/callback"
    )
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"

    # Mutagen agent version pinned in env-template Dockerfiles. Exposed via
    # the /cli/agents/{id}/sync-runtime endpoint so the CLI can refuse to
    # start when the locally-installed Mutagen disagrees with the platform.
    MUTAGEN_VERSION: str = "0.18.1"
    # Platform API version advertised to the CLI alongside the Mutagen pin.
    PLATFORM_API_VERSION: str = "1.0"
    # Minimum cinna-cli version the platform's setup flow supports. Embedded in
    # the `curl | python3` bootstrap script: when a locally-installed `cinna` is
    # older than this, the bootstrap prints upgrade instructions and stops
    # instead of invoking a subcommand the old CLI doesn't have (which would
    # otherwise fail with a confusing "No such command" error). Bump this when a
    # setup-flow change requires a newer CLI.
    MINIMUM_CLI_VERSION: str = "0.2.3"

    # ── Environment console (web terminal + logs follow) ─────────────────
    # Idle timeout for an interactive PTY shell: the env-core /shell/pty
    # endpoint and the backend terminal tunnel both auto-close a terminal
    # after this many seconds of no inbound keystrokes, so a forgotten
    # browser tab cannot leave an orphaned shell holding the env warm.
    ENV_TERMINAL_IDLE_TIMEOUT_SECONDS: int = 900  # 15 minutes
    # Max concurrent consoles attached to a single environment. SHARED across
    # both console kinds (terminal + logs) by design — one knob avoids per-kind
    # accounting and 3 is generous for an operator. Intentional trade-off: 3
    # open Logs tabs on the same env block a 4th console of EITHER kind (incl.
    # Terminal). Split into per-kind caps if that proves annoying in practice.
    ENV_CONSOLE_MAX_PER_ENV: int = 3
    # Max concurrent consoles a single user may hold across all of their
    # environments (also shared across kinds).
    ENV_CONSOLE_MAX_PER_USER: int = 10
    # Per-user console open-rate cap (opens allowed within the sliding window).
    ENV_CONSOLE_OPEN_RATE_LIMIT: int = 10
    ENV_CONSOLE_OPEN_RATE_WINDOW_SECONDS: int = 60
    # Initial logs tail snapshot clamp (lines).
    ENV_CONSOLE_LOGS_TAIL_DEFAULT: int = 200
    ENV_CONSOLE_LOGS_TAIL_MAX: int = 5000

    BACKEND_CORS_ORIGINS: Annotated[
        list[AnyUrl] | str, BeforeValidator(parse_cors)
    ] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def backend_base_url(self) -> str:
        """Public origin for API URLs handed outside the platform, no trailing slash.

        Single resolution point, so an operator has one knob to turn and the
        URLs cannot drift apart from each other. Covers inbound webhooks
        (task triggers, agent hooks, server channels), the consumer-facing
        Agent REST API base, and signed A2A attachment links.
        """
        base = (
            self.BACKEND_BASE_URL
            or self.WEBHOOK_BASE_URL
            or self.FRONTEND_HOST
            or "https://localhost"
        )
        return base.rstrip("/")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def webhook_base_url(self) -> str:
        """Alias of :attr:`backend_base_url`, kept for the webhook call sites."""
        return self.backend_base_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_cors_origins(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS] + [
            self.FRONTEND_HOST
        ]

    PROJECT_NAME: str
    SENTRY_DSN: HttpUrl | None = None
    POSTGRES_SERVER: str
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str = ""
    POSTGRES_DB: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_SERVER,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    # Test mode. Defaults to False (full production behavior). The pytest
    # harness (`tests/conftest.py`) flips this to True *before* importing the
    # app so the lifespan can skip background schedulers and other heavy startup
    # that tests never need (and which would otherwise bind jobs to the real
    # application DB engine — an isolation escape). Never set in production.
    TESTING: bool = False

    # Test database settings (separate DB for pytest)
    TEST_DB_SERVER: str | None = None
    TEST_DB_PORT: int = 5432
    TEST_DB_NAME: str = "app_test"
    TEST_DB_USER: str | None = None
    TEST_DB_PASSWORD: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def TEST_SQLALCHEMY_DATABASE_URI(self) -> PostgresDsn | None:
        if not self.TEST_DB_SERVER:
            return None
        return PostgresDsn.build(
            scheme="postgresql+psycopg",
            username=self.TEST_DB_USER or self.POSTGRES_USER,
            password=self.TEST_DB_PASSWORD or self.POSTGRES_PASSWORD,
            host=self.TEST_DB_SERVER,
            port=self.TEST_DB_PORT,
            path=self.TEST_DB_NAME,
        )

    SMTP_TLS: bool = True
    SMTP_SSL: bool = False
    SMTP_PORT: int = 587
    SMTP_HOST: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: EmailStr | None = None
    EMAILS_FROM_NAME: str | None = None

    @model_validator(mode="after")
    def _set_default_emails_from(self) -> Self:
        if not self.EMAILS_FROM_NAME:
            self.EMAILS_FROM_NAME = self.PROJECT_NAME
        return self

    EMAIL_RESET_TOKEN_EXPIRE_HOURS: int = 48

    # ── Email confirmation (anti-abuse outbound-email gate) ──────────────
    # Expiry of the JWT confirm-email token (mirrors EMAIL_RESET_TOKEN_EXPIRE_HOURS).
    EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS: int = 48
    # Minimum seconds between resend-confirmation emails for a given user.
    CONFIRMATION_EMAIL_COOLDOWN_SECONDS: int = 300  # 5 min between resends
    # Minimum seconds between password-recovery emails for a given user.
    # Stored as a column on the target user row (the recovery endpoint is
    # public/by-email and may be served by multiple workers, so an in-memory
    # throttle cannot rate-limit it reliably).
    PASSWORD_RECOVERY_EMAIL_COOLDOWN_SECONDS: int = 300  # 5 min between recovery sends
    # Agent-creation caps keyed on confirmation status. Superusers are
    # unlimited (short-circuit on is_superuser — no constant).
    AGENT_LIMIT_UNCONFIRMED: int = 5
    AGENT_LIMIT_CONFIRMED: int = 50

    @computed_field  # type: ignore[prop-decorator]
    @property
    def emails_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.EMAILS_FROM_EMAIL)

    EMAIL_TEST_USER: EmailStr = "test@example.com"
    FIRST_SUPERUSER: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    # Google OAuth Configuration
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str = "http://localhost:5173/auth/google/callback"
    # Separate redirect URI for credential OAuth (to differentiate from user OAuth)
    GOOGLE_CREDENTIALS_REDIRECT_URI: str = "http://localhost:5173/credentials/oauth/callback"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    # Auth domain whitelist - comma-separated list of allowed domains for new user registration
    # Example: "example.com,company.org" - only emails from these domains can register
    # Admin can still create users with any email
    AUTH_WHITELIST_USER_DOMAINS: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def auth_whitelist_domains(self) -> list[str]:
        """Parse comma-separated domains into list"""
        if not self.AUTH_WHITELIST_USER_DOMAINS:
            return []
        return [d.strip().lower() for d in self.AUTH_WHITELIST_USER_DOMAINS.split(",") if d.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allow_user_email_change(self) -> bool:
        """Allow users to change their email. Disabled when domain whitelist is active."""
        return len(self.auth_whitelist_domains) == 0

    # Role assigned to newly created NON-superuser accounts, on both password
    # signup and Google OAuth first login. Creation-time only — it never touches
    # existing users and never overrides an explicit caller-provided role or the
    # superuser ⇒ ``admin`` mapping. Unset/empty falls back to ``agent-user`` via
    # ``env_ignore_empty=True``; a present-but-invalid value fails loudly at
    # startup (Literal). ``admin`` is intentionally not allowed here to preserve
    # the role ⇔ is_superuser invariant. Values mirror ``UserRole`` enum members
    # (UserRole.USER.value / UserRole.DEVELOPER.value) — keep them in sync.
    DEFAULT_USER_ROLE: Literal["agent-user", "agent-developer"] = "agent-user"

    # Google AI Configuration (for ADK agents)
    GOOGLE_API_KEY: str | None = None

    # AI Functions Provider Configuration
    # Comma-separated list of providers to try in order (cascade fallback)
    # Supported: "gemini", "openai-compatible", "openai"
    # Example: "openai,gemini" - try openai first, fall back to gemini
    AI_FUNCTIONS_PROVIDERS: str = "gemini"

    # OpenAI-compatible provider settings (for AI functions)
    # Used when "openai-compatible" is in AI_FUNCTIONS_PROVIDERS
    OPENAI_COMPATIBLE_BASE_URL: str | None = None
    OPENAI_COMPATIBLE_API_KEY: str | None = None
    OPENAI_COMPATIBLE_MODEL: str = "gpt-4o-mini"

    # OpenAI direct provider settings (for AI functions)
    # Used when "openai" is in AI_FUNCTIONS_PROVIDERS
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ai_functions_provider_list(self) -> list[str]:
        """Parse comma-separated providers into ordered list"""
        return [p.strip().lower() for p in self.AI_FUNCTIONS_PROVIDERS.split(",") if p.strip()]

    # Environment Management
    # Paths for environment templates and instances
    # Default for local dev: "backend/app/env-templates" and "backend/app/agent-environments"
    # Default for Docker: "app/env-templates" and "/agent-environments"
    ENV_TEMPLATES_DIR: str = "backend/app/env-templates"
    ENV_INSTANCES_DIR: str = "backend/app/agent-environments"

    # Host path to agent environments (for docker-compose volume mounts)
    # This is the actual path on the Docker host machine
    # For Docker dev: The host sees it as "./backend/agent-environments" (relative to project root)
    # We need absolute path from host perspective for volume mounts
    HOST_AGENT_ENVIRONMENTS_DIR: str | None = None

    # Agent Environment Resource Limits
    AGENT_ENV_CPU_LIMIT: str = "1.0"
    AGENT_ENV_MEMORY_LIMIT: str = "512M"
    AGENT_ENV_CPU_RESERVATION: str = "0.25"
    AGENT_ENV_MEMORY_RESERVATION: str = "128M"

    # Docker Network
    DOCKER_NETWORK_NAME: str = "agent-bridge"

    # Container-reachable backend origin (Docker network service name).
    # Injected into each agent env's .env as BACKEND_URL, and used to rewrite
    # agent_api proxy URLs so consumer containers call the backend over the
    # internal network instead of the public FRONTEND_HOST.
    AGENT_ENV_BACKEND_URL: str = "http://backend:8000"

    # Agent-API caller-identity token (L2).
    # A narrow, audience-restricted JWT (aud="agent_api_caller", sub=owner) that
    # is auto-injected into a consumer env's credentials.json so the agent-api
    # proxy can attribute calls to the install owner. It is RE-MINTED on every
    # credential sync (env start / resync), so the TTL only needs to comfortably
    # exceed the worst-case interval between syncs — a long-idle env whose token
    # lapses before its next sync simply degrades to anonymous. Chosen generously
    # (30 days). Revocation lives at the grant layer, not the token, so this is
    # NOT placed on the pre-stream refresh hook.
    AGENT_API_IDENTITY_TOKEN_EXPIRE_DAYS: int = 30

    # Agent-environment internal token (AGENT_AUTH_TOKEN).
    # The per-environment JWT minted into each container's .env. It is a scoped,
    # audience-restricted token (aud/token_type="agent_env", env_id, agent_id)
    # that authenticates ONLY via AgentEnvContextDep and is rejected by the
    # generic CurrentUser dependency. It is RE-MINTED (rotated) on every
    # configure (create / start / restart / rebuild), which also rotates the
    # per-env auth_token_hash — so the HASH is the real, immediate revocation
    # anchor; this TTL is only a backstop for a token whose env later vanishes.
    # Set to 1 year so continuously-running "always_on" environments (which never
    # idle-suspend and have no background token-refresh job) don't hit an expiry
    # cliff between rebuilds; revocation does not depend on it.
    # (Was effectively a 10-year plain owner JWT before the scoping hardening.)
    AGENT_ENV_TOKEN_EXPIRE_DAYS: int = 365

    # Agent-environment token back-compat grace window.
    # During this window after deploy, AgentEnvContextDep ALSO accepts
    # old-format env tokens (plain owner JWTs minted before the scoping change)
    # that present an X-Agent-Env-Id header, so already-running environments keep
    # working until the deploy-time bulk rebuild rotates them to the new format.
    # Set to False (after the bulk rebuild sweep completes) to harden fully so
    # only new-format env tokens authenticate.
    AGENT_ENV_TOKEN_ACCEPT_LEGACY: bool = True

    # Agent Authentication
    # Token for backend to authenticate with agent containers
    AGENT_AUTH_TOKEN: str = secrets.token_urlsafe(32)

    # Port Allocation (deprecated - using network names instead)
    # Kept for backward compatibility
    AGENT_PORT_RANGE_START: int = 8000
    AGENT_PORT_RANGE_END: int = 9000

    # Default Agent Environment Configuration
    # These are used when creating new agents
    DEFAULT_AGENT_ENV_NAME: str = "python-env-advanced"
    DEFAULT_AGENT_ENV_VERSION: str = "1.0.0"

    # Admin Environment Management
    ADMIN_BULK_REBUILD_CONCURRENCY: int = 4  # Max parallel env rebuilds during bulk operation
    ADMIN_ENV_MAX_BULK_SIZE: int = 200  # Max environment IDs per bulk rebuild request

    # ── Account CLI escape hatch (Phase 3) ──────────────────────────────
    # Limits for the generic ``cinna api <METHOD> <path>`` proxy. The hatch
    # targets JSON control-plane calls, not file transfer or streaming.
    ACCOUNT_API_PROXY_MAX_BODY_BYTES: int = 1_048_576  # 1 MiB request cap → 413
    ACCOUNT_API_PROXY_MAX_RESPONSE_BYTES: int = 8_388_608  # 8 MiB response cap → 502
    ACCOUNT_API_PROXY_RATE_LIMIT_PER_MIN: int = 120  # per-account-token backstop

    # ── Server channels (inbound webhook) ───────────────────────────────
    # The channel webhook is unauthenticated at the platform layer — the
    # unguessable token in the path plus the adapter's signature check are the
    # gate. These two limits bound what an unverified caller can cost us
    # BEFORE verification runs: the body cap applies to the read itself, and
    # the rate limit is keyed on the webhook token so one channel being probed
    # cannot starve the others.
    SERVER_CHANNEL_WEBHOOK_MAX_BODY_BYTES: int = 262_144  # 256 KiB → 413
    SERVER_CHANNEL_WEBHOOK_RATE_LIMIT_PER_MIN: int = 120

    # Admin debug panel: recent inbound/outbound events held per channel, in
    # process memory only (see channel_debug_buffer.py). Bounded twice — a ring
    # buffer per channel, and a clamp on captured message text — so a busy or
    # hostile channel cannot grow it without limit. Never persisted.
    SERVER_CHANNEL_DEBUG_BUFFER_SIZE: int = 50
    SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS: int = 2_000

    # App MCP is a channel too, but an ``authenticated`` one: its policy is
    # consulted in ``app.mcp.app_token_verifier`` on *every* verified token,
    # which is once per HTTP request to /mcp/app/mcp — every tools/call,
    # tools/list, prompts/list and SSE reconnect. Two DB reads per request is
    # not affordable, so the resolved availability is cached per user id, in
    # process memory, for this many seconds.
    #
    # The TTL is the admin's revocation delay and nothing else: disabling the
    # channel, withdrawing a grant, or a user switching it off takes effect
    # within this window (per backend process — the cache is process-local).
    # Keep it short. The kill switch is the reason the channel exists, and an
    # admin who flips it expects it to bite, not to be advisory for a minute.
    # A value <= 0 disables the cache entirely and is what tests set in order
    # to observe a revocation without sleeping; it is a legitimate, if costly,
    # production setting too.
    APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS: int = 45
    # Bound on the cache's size, so a deployment with many App MCP users (or a
    # burst of expired entries) cannot grow it without limit. Reaching it drops
    # expired entries first and clears the rest only if that was not enough —
    # a cleared cache costs one extra lookup per user, never a wrong answer.
    APP_MCP_AVAILABILITY_CACHE_MAX_ENTRIES: int = 10_000

    # ── Routing traces (auto-routing tuning) ────────────────────────────
    # Durable, superuser-only record of each routing decision — candidates
    # (including rejected ones), rendered prompt, provider attempts, verdict.
    # See ``docs/plans/auto_routing_tuning_plan.md`` §4/§7.
    #
    # RULE FOR EVERY SETTING BELOW — **the dangerous state must not be able to
    # look routine.** These knobs govern how much external users' message text
    # this platform keeps and for how long, so a value that means "retain more"
    # must not be reachable by typing something innocuous, and must not render
    # as an unremarkable number. Two instances to copy: this feature's most
    # dangerous configuration is unbounded retention, so
    # ``ROUTING_TRACE_RETENTION_DAYS`` REJECTS ``0`` at startup instead of
    # reading it as keep-forever (``-1`` is the only spelling, and you have to
    # mean it), and ``routing_trace_scheduler`` logs that ``-1`` as "retention
    # DISABLED" rather than "retention -1 days". Adding a setting here: name its
    # most dangerous value, then make reaching it loud.
    # Gates PERSISTENCE AND ADMIN READS, not in-process capture. Deliberately
    # narrower than "master switch" sounds: the recorder cannot read settings (it
    # must stay free of ``app.*`` imports so ``app/agents/`` can import it), and
    # the live channel debug feed's one-line no-match diagnosis is built from a
    # capture, so short-circuiting capture here would silently degrade a feature
    # that has nothing to do with storage. Off means no rows are written; the
    # per-request recording still runs and still feeds the debug panel.
    #
    # A READ gate as well. It was once consulted in exactly one place — the
    # persist path — so turning it off left the admin API happily serving up to
    # ``ROUTING_TRACE_RETENTION_DAYS`` of stored rows, message text included,
    # with no notice: the same asymmetry deliberately rejected for the text flag
    # below. Now the trace list comes back empty and a detail fetch 404s, each
    # carrying ``TRACING_DISABLED_NOTICE``.
    #
    # **It hides; it does not erase**, and ``DELETE /api/v1/admin/routing/traces``
    # deliberately KEEPS WORKING while this is off — otherwise the obvious first
    # move under privacy pressure would also remove the only way to delete the
    # rows already written.
    ROUTING_TRACE_ENABLED: bool = True
    # The one genuinely policy-bearing switch here. Server Channels otherwise
    # keeps inbound message text out of the database; a routing trace without
    # the message is close to useless for tuning, so it is stored — clamped,
    # TTL'd, superuser-only, and behind this flag. With it off, the trace still
    # carries ``message_sha256`` plus the candidate set and the verdict, which
    # is the diagnosis that matters most.
    #
    # **It reaches further than ``message_text``, and it is enforced by an
    # allowlist rather than an inventory.** With the flag off, the ``stages``
    # payload is projected through a list of explicitly-named safe fields
    # (``routing_trace.SAFE_STAGE_FIELDS``) on the write path *and* the read
    # path, from one definition so the two cannot drift. Anything not named
    # there is withheld by default — including fields added after this gate was
    # written, and including fields nobody realised had started carrying the
    # sender's words.
    #
    # That polarity is the whole design. This gate was enforced three times by
    # enumerating the tainted fields, and three times the enumeration turned out
    # to be one field short; sender text is a taint that *propagates* into new
    # fields through ordinary, reviewable-looking changes. An allowlist inverts
    # the question from "did we remember every field" — unanswerable — to "is
    # this field safe", which whoever adds a field can answer as they add it.
    # It also removed a privacy property that rested on the byte length of a
    # markdown template: gating on write means template length cannot create a
    # leak in either direction.
    #
    # Do not re-describe this gate by listing the fields it covers, here or
    # anywhere else. A list is a promise with an expiry date — that is precisely
    # what this comment used to be. Describe the mechanism; read
    # ``SAFE_STAGE_FIELDS`` for the current contents.
    #
    # A READ gate as well as a write gate: with it off the admin API stops
    # serving the gated fields on rows already written, because an operator
    # turning this off means "stop showing me this text", not "stop appending to
    # the pile".
    #
    # **It hides; it does not erase.** Text captured before the flag went off
    # stays in the database. Exactly two things remove it: waiting out
    # ``ROUTING_TRACE_RETENTION_DAYS``, or calling
    # ``DELETE /api/v1/admin/routing/traces``. Flipping this flag does NOT purge,
    # and that is deliberate — an accidental toggle would irreversibly destroy
    # diagnostic data, and a privacy control whose misfire is unrecoverable is
    # its own hazard. Hide on flip, erase on explicit request.
    ROUTING_TRACE_STORE_MESSAGE_TEXT: bool = True
    # Retention window enforced by ``routing_trace_scheduler`` (hourly purge).
    #
    # Must be ``>= 1``. ``-1`` — and only ``-1`` — means "keep forever", the
    # escape hatch for a debugging session that must outlive the window. ``0``
    # and every other negative value are **rejected at startup** by
    # ``_validate_routing_trace_retention`` below, which names ``-1`` in the
    # error so an operator who wanted unbounded retention is told how to ask
    # for it, and an operator who typed ``0`` meaning "don't keep this" is told
    # they were about to get the opposite.
    ROUTING_TRACE_RETENTION_DAYS: int = 14
    # Clamp applied at persist time to the free-text *columns* on
    # ``routing_decision`` (``message_text``, ``error``). Free text *inside*
    # ``stages`` never passes through here: the recorder clamps every free-text
    # field it captures as it captures it, using its own ``TRACE_TEXT_MAX_CHARS``
    # constant, which cannot read settings because ``routing_trace.py`` must stay
    # free of ``app.*`` imports. Keep the two in step. Which stage fields those
    # are is the recorder's business and changes as it grows — see ``clamp()``'s
    # call sites rather than trusting a list written here. The default matches
    # ``SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS`` deliberately: the two surfaces
    # show the same text to the same audience and should truncate alike.
    ROUTING_TRACE_TEXT_MAX_CHARS: int = 2_000
    # How much of an App MCP routing decision is written to ``routing_decision``.
    # Applies to ``origin="app_mcp"`` rows and to nothing else — every other
    # origin is governed by ``ROUTING_TRACE_ENABLED`` and
    # ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` alone.
    #
    # Read this table rather than the code. Naming the fields here is the whole
    # point of the comment: this setting was **removed once** because a reader
    # could not tell from it what a value did (it was unreachable at every
    # value, including ``off``, so an operator who set it believed they had
    # disabled a capture that was never running). Shipping it again with a
    # description that has to be decoded by tracing ``persist`` would repeat
    # that defect in a new shape.
    #
    #   ``off``       No row at all. ``AppMCPRoutingService.route_message``
    #                 opens no capture, and ``RoutingTraceService.persist``
    #                 refuses any ``app_mcp`` trace that reaches it anyway.
    #                 ``GET /admin/routing/traces?origin=app_mcp`` stays empty.
    #                 Absence, not a swallowed failure: nothing is attempted,
    #                 so nothing can half-succeed.
    #
    #   ``metadata``  (default) The row is written **without the sender's
    #                 words**. Omitted: ``message_text``, and every ``stages``
    #                 field outside ``routing_trace.SAFE_STAGE_FIELDS`` — i.e.
    #                 ``stages[].prompt``, ``stages[].raw_response``,
    #                 ``stages[].reason``, ``stages[].llm_attempts[].error``,
    #                 and any ``candidates[]`` field outside
    #                 ``SAFE_CANDIDATE_FIELDS``. Stored: ``message_sha256``
    #                 (so the row stays replayable and "withheld" stays
    #                 distinguishable from "no message"), ``origin``,
    #                 ``outcome``, ``match_method``, ``channel_id``,
    #                 ``user_id``, ``selected_*``, ``confidence``,
    #                 ``latency_ms``, the row-level ``error``, and
    #                 ``stages[].stage`` / ``.match_method`` /
    #                 ``.matched_pattern`` / ``.confidence`` / ``.runner_up_id``
    #                 / ``.not_run_code`` / ``.candidates`` / ``.llm_attempts``.
    #                 Exactly the set ``ROUTING_TRACE_STORE_MESSAGE_TEXT=False``
    #                 omits, applied to this one origin — one rule, not two.
    #
    #   ``full``      No per-origin narrowing. ``app_mcp`` rows are written
    #                 exactly like a channel's, and
    #                 ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` alone decides whether
    #                 the text is stored.
    #
    # The two settings **AND**; ``metadata`` narrows and never widens, so
    # ``full`` cannot re-open a text gate that ``ROUTING_TRACE_STORE_MESSAGE_TEXT``
    # has closed.
    #
    # Why the default is the narrow one: App MCP routes *every* message, not
    # just thread openings, and it does not sit behind the webhook rate limit
    # that bounds the channel path — so it is the only origin whose write
    # volume is unbounded, and ``message_text`` / ``prompt`` / ``raw_response``
    # are the dominant per-row cost. (For the record, the removal note this
    # replaces claimed the two capture sites in ``channel_routing_service.py``
    # were "both ``origin=server_channel``". They were not, even then: simulate
    # and replay reuse those same two sites with ``origin="simulate"``.)
    ROUTING_TRACE_APP_MCP_MODE: str = ROUTING_TRACE_APP_MCP_METADATA

    # Per-admin cap on the routing-tuning calls that spend real LLM budget:
    # POST /admin/routing/simulate, .../traces/{id}/replay and
    # .../traces/{id}/recommendation share ONE bucket per admin, so rotating
    # between the three routes cannot spend three times this. Process-local like
    # every other consumer of the shared limiter — with N workers the effective
    # ceiling is N x this — because it is a backstop against a stuck UI, not a
    # billing control. The accountability half of the §12 exposure ruling is the
    # ROUTING_SIMULATE_RUN audit row; this is the non-bulk half.
    ROUTING_SIMULATE_RATE_LIMIT_PER_MIN: int = 10

    @model_validator(mode="after")
    def _validate_routing_trace_retention(self) -> Self:
        """Reject a retention window that would silently keep traces forever.

        Rejecting rather than clamping to the default is the point. A startup
        failure costs an operator seconds and tells them exactly what to fix;
        substituting ``14`` for someone who typed ``0`` meaning "don't retain
        this" leaves them believing something false about their own deployment,
        and the thing they are wrong about is how long external users' message
        text sits in their database. It fails toward keeping *more* text than
        intended, which is the one direction this knob must not fail in.
        """
        days = self.ROUTING_TRACE_RETENTION_DAYS
        if days == ROUTING_TRACE_RETENTION_FOREVER or days >= 1:
            return self
        raise ValueError(
            f"ROUTING_TRACE_RETENTION_DAYS must be at least 1, or exactly "
            f"{ROUTING_TRACE_RETENTION_FOREVER} to keep routing traces forever "
            f"(got {days}). Routing traces can hold external senders' message "
            f"text, so a retention window is not optional: "
            f"{ROUTING_TRACE_RETENTION_FOREVER} is the only way to disable "
            f"expiry, and it is deliberately not spelled 0. If you meant "
            f"'store nothing', set ROUTING_TRACE_STORE_MESSAGE_TEXT=False to "
            f"keep traces without the message text, or "
            f"ROUTING_TRACE_ENABLED=False to stop writing traces entirely."
        )

    @model_validator(mode="after")
    def _validate_routing_trace_app_mcp_mode(self) -> Self:
        """Reject an unknown ``ROUTING_TRACE_APP_MCP_MODE`` at startup.

        Rejecting rather than falling back, for the same reason the retention
        validator above rejects: an unrecognised value would degrade to the
        ``metadata`` branch (it is neither ``off`` nor ``full``), so an operator
        who typed ``none`` or ``disabled`` meaning "stop recording App MCP
        decisions" would get rows they believe do not exist. That is precisely
        the failure this setting was withdrawn over the first time — a control
        that appears to do more, or less, than it does — and it is cheaper to
        fail a boot than to discover it from a table.
        """
        mode = self.ROUTING_TRACE_APP_MCP_MODE
        if mode in ROUTING_TRACE_APP_MCP_MODES:
            return self
        raise ValueError(
            f"ROUTING_TRACE_APP_MCP_MODE must be one of "
            f"{', '.join(ROUTING_TRACE_APP_MCP_MODES)} (got {mode!r}). "
            f"{ROUTING_TRACE_APP_MCP_OFF!r} writes no app_mcp routing traces at "
            f"all, {ROUTING_TRACE_APP_MCP_METADATA!r} writes them without the "
            f"sender's message text or any model prompt/response, and "
            f"{ROUTING_TRACE_APP_MCP_FULL!r} writes them like any other origin. "
            f"To stop writing traces for every origin, set "
            f"ROUTING_TRACE_ENABLED=False instead."
        )

    # ── Two-Factor Authentication (MFA) ────────────────────────────────
    # Settings that govern the WebAuthn passkey + TOTP authenticator-app
    # 2FA flows.  See ``docs/drafts/user-2fa-passkeys-totp_plan.md``.
    MFA_CHALLENGE_TTL_SECONDS: int = 300
    MFA_MAX_ATTEMPTS_PER_CHALLENGE: int = 5
    MFA_RECOVERY_CODE_COUNT: int = 8
    # 8 characters → ``XXXX-XXXX`` (clean two-block presentation in the
    # UI).  10 produces an awkward ``XXXX-XXXX-XX`` chunking.
    MFA_RECOVERY_CODE_LENGTH: int = 8
    MFA_WEBAUTHN_RP_NAME: str = "Cinna"
    # Override RP ID; when unset, falls back to ``urlparse(FRONTEND_HOST).hostname``.
    MFA_WEBAUTHN_RP_ID: str | None = None
    MFA_TOTP_ISSUER: str = "Cinna"
    # Server-side allowlist for the "Do not ask on this device" duration.
    # The MFA service rejects any ``remember_device_days`` not in this set
    # (defence-in-depth behind the ``Literal[1, 7, 30]`` on the schema —
    # keep the two in sync).
    MFA_TRUSTED_DEVICE_ALLOWED_DAYS: list[int] = [1, 7, 30]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mfa_webauthn_rp_id(self) -> str:
        """Resolve the WebAuthn relying-party ID.

        Falls back to ``FRONTEND_HOST``'s hostname when not explicitly set.
        ``localhost`` is allowed (special-cased by browsers / py_webauthn).
        """
        if self.MFA_WEBAUTHN_RP_ID:
            return self.MFA_WEBAUTHN_RP_ID
        from urllib.parse import urlparse
        host = urlparse(self.FRONTEND_HOST).hostname or "localhost"
        return host

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mfa_webauthn_expected_origin(self) -> str:
        """Origin string used to validate WebAuthn assertions.

        Strips the trailing slash from ``FRONTEND_HOST`` if present, since
        the browser sends the bare origin (``scheme://host[:port]``) in the
        ``clientDataJSON``.
        """
        return self.FRONTEND_HOST.rstrip("/")

    # Desktop App Authentication
    DESKTOP_AUTH_ENABLED: bool = True
    DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    # Reuse-grace window for refresh-token rotation (OWASP / RFC 9700 §4.14.2).
    # A revoked token re-presented within this window after rotation is treated
    # as a benign lost-rotation-response retry (re-rotate from the same family)
    # instead of a theft replay (revoke the whole family). Native apps that get
    # suspended mid-refresh (iOS backgrounding, request timeouts) never persist
    # the successor token and would otherwise be force-logged-out.
    DESKTOP_REFRESH_TOKEN_REUSE_GRACE_SECONDS: int = 60

    # Mobile App Authentication (parallel /app-auth surface, shared backing).
    # Token lifetimes are shared with the desktop flow (same DesktopAuthService).
    APP_AUTH_ENABLED: bool = True

    # App Sync (native-client data sync — zero-knowledge document store)
    # Limits and quotas are enforced on CIPHERTEXT bytes (the server never
    # sees plaintext). See docs/application/app_sync/app_sync_tech.md.
    APP_SYNC_MAX_PAYLOAD_BYTES: int = 1024 * 1024  # 1 MiB per record
    APP_SYNC_MAX_RECORDS_PER_PUSH: int = 500
    APP_SYNC_MAX_PULL_LIMIT: int = 500  # pull pagination ceiling (independent of push batch)
    APP_SYNC_QUOTA_BYTES: int = 256 * 1024 * 1024  # 256 MiB per user
    APP_SYNC_QUOTA_RECORDS: int = 50_000
    APP_SYNC_TOMBSTONE_RETENTION_DAYS: int = 180
    APP_SYNC_PAIRING_TTL_SECONDS: int = 300  # 5 minutes

    # Run Command Execution Settings
    RUN_COMMAND_TIMEOUT_SECONDS: int = 300
    RUN_COMMAND_MAX_OUTPUT_BYTES: int = 262144  # 256 KB

    # AI Model Discovery (per-credential available-model cache cron).
    # Different API keys can see different models, so the available-model list
    # is polled per AICredential against each provider's native /models
    # endpoint and cached on the credential. Model lists change rarely → daily.
    MODEL_DISCOVERY_ENABLED: bool = True
    MODEL_DISCOVERY_INTERVAL_HOURS: int = 24

    # Bundle auto-update convergence sweep.
    # Installs with ``update_mode="automatic"`` whose environment is idle
    # (no env at all, suspended, or stopped) are converged onto the bundle's
    # latest revision by a dedicated background sweep — the suspension-time
    # hook only covers the running → suspended transition, so an environment
    # that was already suspended when a revision was published would otherwise
    # never update. The same sweep also runs bundle-scoped right after a
    # publish (fast path). ``RETRY_BACKOFF_HOURS`` keeps a persistently failing
    # install from being retried on every sweep.
    BUNDLE_AUTO_UPDATE_ENABLED: bool = True
    BUNDLE_AUTO_UPDATE_INTERVAL_MINUTES: int = 10
    BUNDLE_AUTO_UPDATE_BATCH_LIMIT: int = 50
    BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS: int = 6

    # Bundle / App Data Storage (Phase 1 — agent bundles & installs)
    # ``BUNDLE_STORAGE_DIR`` holds bundle revision snapshots:
    #   <BUNDLE_STORAGE_DIR>/<bundle_id>/<revision_number>/
    # ``APP_DATA_STORAGE_DIR`` holds per-(user, bundle) persistent volumes:
    #   <APP_DATA_STORAGE_DIR>/<user_id>/<bundle_id>/{storage,uploads,cache}
    # Both are created lazily by the services with mode 0o755.
    BUNDLE_STORAGE_DIR: str = "/app/data/bundles"
    APP_DATA_STORAGE_DIR: str = "/app/data/app-data"

    # Host-side path to ``APP_DATA_STORAGE_DIR`` for docker-compose volume
    # mounts. Mirrors ``HOST_AGENT_ENVIRONMENTS_DIR`` for Docker-in-Docker
    # setups; falls back to ``APP_DATA_STORAGE_DIR`` when None (local dev,
    # backend running on the host directly).
    HOST_APP_DATA_DIR: str | None = None

    # File Upload Settings
    UPLOAD_BASE_PATH: str = "/app/data/uploads"
    UPLOAD_MAX_FILE_SIZE_MB: int = 100
    UPLOAD_MAX_USER_STORAGE_GB: int = 10
    UPLOAD_RATE_LIMIT_PER_MINUTE: int = 10

    # Allowed mime types (comma-separated). Entries may be exact mime types
    # (e.g. ``application/pdf``) or wildcard patterns ending in ``/*``
    # (e.g. ``text/*`` to allow any text subtype).
    UPLOAD_ALLOWED_MIME_TYPES: str = (
        # All text formats (plain, markdown, csv, html, xml, source code, etc.)
        "text/*,"
        # PDFs and images
        "application/pdf,"
        "image/png,image/jpeg,image/gif,image/webp,"
        # Archives
        "application/zip,application/x-tar,application/gzip,"
        # Structured data
        "application/json,application/xml,"
        # Whole email messages. A forwarded mail's own attachments arrive
        # loose, but a forward that carries none is materialised as a ``.eml``
        # instead of being lost (the fallback in
        # ``EmailPollingService._extract_attachments``); without this entry
        # that fallback would be skipped as ``type_not_allowed``. Note this
        # allowlist is deployment-wide — it also permits ``.eml`` on the web
        # upload, agent ``<cinna_attach>`` and A2A paths. Deliberate: the
        # platform never parses an upload, it only stores it and hands it to
        # the agent, so a mail file is no more privileged than a PDF.
        "message/rfc822,"
        # Microsoft Office (legacy + OOXML)
        "application/msword,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.ms-excel,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "application/vnd.ms-powerpoint,"
        "application/vnd.openxmlformats-officedocument.presentationml.presentation,"
        # OpenDocument
        "application/vnd.oasis.opendocument.text,"
        "application/vnd.oasis.opendocument.spreadsheet,"
        "application/vnd.oasis.opendocument.presentation,"
        # Rich Text
        "application/rtf"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_mime_types(self) -> set[str]:
        """Parse comma-separated mime types into set. Entries may be exact
        types or wildcard patterns ending in ``/*``."""
        return set(mime.strip() for mime in self.UPLOAD_ALLOWED_MIME_TYPES.split(","))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upload_max_file_size_bytes(self) -> int:
        """Convert MB to bytes"""
        return self.UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upload_max_user_storage_bytes(self) -> int:
        """Convert GB to bytes"""
        return self.UPLOAD_MAX_USER_STORAGE_GB * 1024 * 1024 * 1024

    # --- Server-channel inbound attachments ------------------------------
    #
    # Files arriving from *outside* the platform (Google Chat, polled email)
    # are bounded more tightly than a web upload: they arrive unattended, on
    # the platform's only unauthenticated ingress, and nobody is watching a
    # progress bar. These caps are the whole of the feature's bound — there
    # is deliberately no per-channel `allow_attachments` switch.
    #
    # Per-file cap, deliberately BELOW the 100MB web-upload cap: 25MB is also
    # where most mail servers stop.
    CHANNEL_ATTACHMENT_MAX_FILE_MB: int = Field(default=25, ge=1)
    # How many attachments one inbound message may carry. Refs beyond this are
    # skipped WITHOUT being fetched — and, on email, without being *retained*
    # either: the parser measures the excess parts so the manifest stays honest
    # and then drops their bytes, because on that transport the cost the
    # promise is about is the decoded copy, not a download.
    CHANNEL_ATTACHMENT_MAX_PER_MESSAGE: int = Field(default=10, ge=1)
    # Aggregate byte cap across one inbound message, so ten files that each
    # fit cannot jointly blow past what one message is allowed to bring.
    #
    # It is also the lever on peak memory, though not one-for-one: the fetch
    # concurrency is derived from it (aggregate ÷ per-file), so a message's
    # true peak is roughly `aggregate + concurrency × per-file` — 100MB with
    # these defaults. Nothing caps that across *simultaneous* messages, so a
    # deployment expecting many concurrent attachment-bearing webhooks should
    # size this against its worker memory rather than against one message.
    CHANNEL_ATTACHMENT_MAX_AGGREGATE_MB: int = Field(default=50, ge=1)
    # Fetch deadline for a transport that hands out handles rather than bytes
    # (Google Chat media). **Spent in two places, and the second is the one
    # that decides the outcome:** the adapter uses it as the per-request HTTP
    # timeout, and ``ChannelAttachmentService`` uses the same number as the
    # deadline for the *whole message's* fetch phase. So this is a
    # whole-message budget, not a per-file allowance — ten attachments share
    # these thirty seconds, they do not get thirty each.
    #
    # That is stricter than a naive reading and deliberately so: step 6.5 runs
    # inside a webhook Google expects acked in about thirty seconds, and a
    # budget that scaled with the attachment count would miss the ack window
    # rather than drop a file. The cost is the tail of a large legitimate
    # message, which is skipped as ``fetch_budget_exhausted`` — a reason code
    # kept distinct from ``timeout`` precisely so this constant is where the
    # operator looks.
    CHANNEL_ATTACHMENT_FETCH_TIMEOUT_SECONDS: int = Field(default=30, ge=1)
    # Multiple of the per-message aggregate that one poll tick may hold in
    # memory across all fetched messages. Beyond it, later messages'
    # attachments are dropped to refs rather than buffered — a mailbox with a
    # backlog of large mail must not become an OOM.
    CHANNEL_ATTACHMENT_POLL_BUDGET_MULTIPLIER: int = Field(default=4, ge=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def channel_attachment_max_file_bytes(self) -> int:
        """Convert MB to bytes"""
        return self.CHANNEL_ATTACHMENT_MAX_FILE_MB * 1024 * 1024

    @computed_field  # type: ignore[prop-decorator]
    @property
    def channel_attachment_max_aggregate_bytes(self) -> int:
        """Convert MB to bytes"""
        return self.CHANNEL_ATTACHMENT_MAX_AGGREGATE_MB * 1024 * 1024

    @computed_field  # type: ignore[prop-decorator]
    @property
    def channel_attachment_poll_budget_bytes(self) -> int:
        """Per-poll-tick in-memory attachment budget, in bytes."""
        return (
            self.channel_attachment_max_aggregate_bytes
            * self.CHANNEL_ATTACHMENT_POLL_BUDGET_MULTIPLIER
        )

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        if value == "changethis":
            message = (
                f'The value of {var_name} is "changethis", '
                "for security, please change it, at least for deployments."
            )
            if self.ENVIRONMENT == "local":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)
        self._check_default_secret("ENCRYPTION_KEY", self.ENCRYPTION_KEY)
        self._check_default_secret("POSTGRES_PASSWORD", self.POSTGRES_PASSWORD)
        self._check_default_secret("FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD)

        return self


settings = Settings()  # type: ignore
