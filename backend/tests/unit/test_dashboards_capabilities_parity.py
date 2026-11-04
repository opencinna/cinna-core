"""The backend's mirrored endpoint-runner limits equal env-core's enforcing values."""
from app.services.dashboards import capabilities
from core.server import endpoint_runner


def test_mirrored_timeout_default_matches_env_core() -> None:
    assert capabilities.ENDPOINT_DEFAULT_TIMEOUT_S == endpoint_runner.DEFAULT_TIMEOUT_S


def test_mirrored_timeout_max_matches_env_core() -> None:
    assert capabilities.ENDPOINT_MAX_TIMEOUT_S == endpoint_runner.MAX_TIMEOUT_S


def test_mirrored_stdout_cap_matches_env_core() -> None:
    assert capabilities.ENDPOINT_MAX_STDOUT_BYTES == endpoint_runner.MAX_STDOUT_BYTES
