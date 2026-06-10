"""
Unit test for the ``AgentEnvironmentError`` exception hierarchy.

Verifies the status codes and default messages on each exception class. The
403/404 HTTP behavior these map to is also covered by the ownership scenarios in
``tests/api/agent_environments/test_agent_environments.py``.
"""
from app.services.environments.environment_service import (
    AgentEnvironmentError,
    AgentNotFoundError,
    EnvironmentCredentialError,
    EnvironmentNotFoundError,
    EnvironmentPermissionDeniedError,
)


def test_environment_exception_classes() -> None:
    # Base class
    err = AgentEnvironmentError("something went wrong", status_code=400)
    assert err.status_code == 400
    assert err.message == "something went wrong"
    assert str(err) == "something went wrong"

    # Default status codes
    err = AgentEnvironmentError("oops")
    assert err.status_code == 400

    # EnvironmentNotFoundError
    err = EnvironmentNotFoundError()
    assert err.status_code == 404
    assert err.message == "Environment not found"
    assert isinstance(err, AgentEnvironmentError)

    err = EnvironmentNotFoundError("custom not found message")
    assert err.status_code == 404
    assert err.message == "custom not found message"

    # AgentNotFoundError
    err = AgentNotFoundError()
    assert err.status_code == 404
    assert err.message == "Agent not found"
    assert isinstance(err, AgentEnvironmentError)

    # EnvironmentPermissionDeniedError
    err = EnvironmentPermissionDeniedError()
    assert err.status_code == 403
    assert err.message == "Not enough permissions"
    assert isinstance(err, AgentEnvironmentError)

    # EnvironmentCredentialError
    err = EnvironmentCredentialError("Missing API key")
    assert err.status_code == 400
    assert err.message == "Missing API key"
    assert isinstance(err, AgentEnvironmentError)
