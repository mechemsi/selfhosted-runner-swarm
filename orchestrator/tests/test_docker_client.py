"""Tests for Docker client helpers."""

import pytest

from rorch.docker_client import _parse_running_minutes


class TestParseRunningMinutes:
    def test_seconds(self) -> None:
        assert _parse_running_minutes("30 seconds") == pytest.approx(0.5)

    def test_minutes(self) -> None:
        assert _parse_running_minutes("5 minutes") == 5.0

    def test_hours(self) -> None:
        assert _parse_running_minutes("2 hours") == 120.0

    def test_days(self) -> None:
        assert _parse_running_minutes("1 day") == 1440.0

    def test_singular_forms(self) -> None:
        assert _parse_running_minutes("1 second") == pytest.approx(1 / 60)
        assert _parse_running_minutes("1 minute") == 1.0
        assert _parse_running_minutes("1 hour") == 60.0

    def test_invalid_format(self) -> None:
        assert _parse_running_minutes("") is None
        assert _parse_running_minutes("unknown") is None

    def test_about_prefix(self) -> None:
        # Docker sometimes outputs "About a minute"
        assert _parse_running_minutes("garbage data here") is None
