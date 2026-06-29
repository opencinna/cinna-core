import secrets
import warnings
from typing import Annotated, Any, Literal

from pydantic import (
    AnyUrl,
    BeforeValidator,
    EmailStr,
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
