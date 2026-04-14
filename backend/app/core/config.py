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
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"

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

    # File Upload Settings
    UPLOAD_BASE_PATH: str = "/app/data/uploads"
    UPLOAD_MAX_FILE_SIZE_MB: int = 100
    UPLOAD_MAX_USER_STORAGE_GB: int = 10
    UPLOAD_RATE_LIMIT_PER_MINUTE: int = 10

    # Allowed mime types (comma-separated)
    UPLOAD_ALLOWED_MIME_TYPES: str = (
        "application/pdf,text/plain,text/csv,"
        "application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "image/png,image/jpeg,image/gif,image/webp,"
        "application/zip,application/x-tar,application/gzip,"
        "application/json,text/javascript,text/html,text/x-python"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_mime_types(self) -> set[str]:
        """Parse comma-separated mime types into set"""
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
