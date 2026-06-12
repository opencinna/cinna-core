"""Unit tests for per-type schedule frequency validation — pure logic.

Covers ``AgentSchedulerService._minimum_interval_minutes`` (smallest gap between
consecutive CRON fire times) and ``validate_frequency`` (the per-type floor:
``static_prompt`` = 10 min, ``script_trigger`` = none). No database, no HTTP.

Regression anchor: "every 40 minutes" (``*/40``) must be accepted for both
types — it has a real 20-minute minimum gap, comfortably above the 10-minute
static floor — fixing the bug where it was wrongly rejected as "too frequent".
"""
from __future__ import annotations

import pytest

from app.services.agents.agent_scheduler_service import (
    AgentSchedulerService,
    ScheduleError,
)


@pytest.mark.parametrize(
    "cron, expected",
    [
        ("* * * * *", 1.0),       # every minute
        ("*/5 * * * *", 5.0),     # every 5 min
        ("*/10 * * * *", 10.0),   # every 10 min
        ("*/30 * * * *", 30.0),   # every 30 min
        ("*/40 * * * *", 20.0),   # ":00 and :40" → 20 min real gap
        ("0 * * * *", 60.0),      # hourly
        ("0 9 * * *", 1440.0),    # daily
    ],
)
def test_minimum_interval_minutes(cron, expected):
    assert AgentSchedulerService._minimum_interval_minutes(cron) == expected


class TestValidateFrequencyStaticPrompt:
    """static_prompt has a 10-minute floor."""

    @pytest.mark.parametrize(
        "cron",
        ["*/10 * * * *", "*/30 * * * *", "*/40 * * * *", "0 * * * *", "0 9 * * *"],
    )
    def test_allowed(self, cron):
        # Should not raise.
        AgentSchedulerService.validate_frequency(cron, "static_prompt")

    @pytest.mark.parametrize("cron", ["*/5 * * * *", "* * * * *"])
    def test_rejected(self, cron):
        with pytest.raises(ScheduleError) as exc:
            AgentSchedulerService.validate_frequency(cron, "static_prompt")
        assert "minimum interval" in exc.value.message
        assert "static prompt" in exc.value.message


class TestValidateFrequencyScriptTrigger:
    """script_trigger has NO floor — any cadence is accepted."""

    @pytest.mark.parametrize(
        "cron",
        ["* * * * *", "*/5 * * * *", "*/10 * * * *", "*/40 * * * *", "0 9 * * *"],
    )
    def test_all_allowed(self, cron):
        # Should not raise for any frequency.
        AgentSchedulerService.validate_frequency(cron, "script_trigger")


def test_every_40_minutes_is_the_regression_case():
    """The exact bug report: 'every 40 minutes' was rejected; now it passes."""
    cron = "*/40 * * * *"
    AgentSchedulerService.validate_frequency(cron, "static_prompt")
    AgentSchedulerService.validate_frequency(cron, "script_trigger")
