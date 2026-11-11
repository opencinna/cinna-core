"""Speech provider adapters for the voice transcription relay.

Everything above this package (session service, WS route, ledger, limits)
talks to :class:`~.base.SpeechAdapter` / :class:`~.base.SpeechStream` only and
never imports a provider SDK. Look adapters up with :func:`.registry.get_adapter`.
"""
