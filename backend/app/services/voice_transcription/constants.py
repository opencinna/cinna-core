"""Fixed facts of the voice transcription relay (protocol v1)."""
from app.models.credentials.ai_credential import AICredentialType

# Provider types that have a speech adapter. Speech-capable additionally
# requires a ``fixed_key`` provider and a registered adapter.
SPEECH_CAPABLE_PROVIDER_TYPES: frozenset[str] = frozenset({AICredentialType.GOOGLE.value})

# 16 kHz x 16-bit mono PCM.
PCM_BYTES_PER_SECOND = 32_000
MAX_FRAME_BYTES = 65_536
PROTOCOL_VERSION = 1
BACKEND_NAME = "core-relay"
