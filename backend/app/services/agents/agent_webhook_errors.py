"""
Exception classes for Agent Webhook Service.

These exceptions carry an HTTP status code so route handlers can translate
them into HTTPException with one line.
"""


class WebhookError(Exception):
    """Base exception for agent webhook service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class WebhookNotFoundError(WebhookError):
    """Webhook not found — used both for missing rows and disabled webhooks
    (to avoid leaking existence on the public endpoint)."""

    def __init__(self, message: str = "Webhook not found"):
        super().__init__(message, status_code=404)


class WebhookValidationError(WebhookError):
    """Create / update payload failed validation."""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class WebhookPermissionError(WebhookError):
    """Caller does not own the agent."""

    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=403)


class WebhookTokenInvalidError(WebhookError):
    """Bearer token missing or does not match the stored ciphertext."""

    def __init__(self, message: str = "Invalid or expired token"):
        super().__init__(message, status_code=401)
