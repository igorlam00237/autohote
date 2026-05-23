"""Tests unitaires des filtres Jinja exposés par dashboard/app.py."""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.unit
class TestHumantimeFilter:
    def _filter(self):
        from app import humantime
        return humantime

    def test_none_returns_dash(self):
        assert self._filter()(None) == "—"

    def test_seconds(self):
        dt = datetime.now(timezone.utc) - timedelta(seconds=10)
        assert self._filter()(dt).startswith("il y a") and "s" in self._filter()(dt)

    def test_minutes(self):
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert "5 min" in self._filter()(dt)

    def test_hours(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=3)
        assert "3 h" in self._filter()(dt)

    def test_more_than_a_day_uses_absolute_format(self):
        dt = datetime.now(timezone.utc) - timedelta(days=2)
        result = self._filter()(dt)
        # Format dd/mm HH:MM
        assert "/" in result and ":" in result

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
        result = self._filter()(dt)
        assert "min" in result


@pytest.mark.unit
class TestFlagFilter:
    def _filter(self):
        from app import language_flag
        return language_flag

    @pytest.mark.parametrize("lang,expected", [
        ("fr", "🇫🇷"),
        ("en", "🇬🇧"),
        ("es", "🇪🇸"),
        ("FR", "🇫🇷"),  # casse ignorée
        ("EN", "🇬🇧"),
    ])
    def test_known_languages(self, lang, expected):
        assert self._filter()(lang) == expected

    def test_unknown_language_fallback(self):
        assert self._filter()("xx") == "🌐"
        assert self._filter()(None) == "🌐"
        assert self._filter()("") == "🌐"
