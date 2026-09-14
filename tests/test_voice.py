from __future__ import annotations

import datetime as dt

from src.voice.dialer import (
    check_calling_window,
    place_call,
    timezone_for_state,
)
from src.voice.signals import detect_hostile, detect_welfare
from src.voice.speech import (
    amount_to_words,
    date_to_words,
    number_to_words,
    ordinal_to_words,
)


class TestSignals:
    def test_hostile_lawyer(self):
        result = detect_hostile("I already told you, my attorney will sue you if you keep calling")
        assert result.detected
        assert "attorney" in result.matches or "sue" in result.matches

    def test_hostile_cease_and_desist(self):
        assert detect_hostile("cease and desist immediately").detected

    def test_no_false_positive_on_polite(self):
        assert not detect_hostile("we'll get you paid by friday, thanks for reaching out").detected

    def test_welfare_english(self):
        assert detect_welfare("honestly, I don't want to live anymore").detected

    def test_welfare_chinese(self):
        assert detect_welfare("我真的不想活了").detected

    def test_welfare_none(self):
        assert not detect_welfare("we hit a cash flow issue").detected


class TestCallingWindow:
    def test_tuesday_10am_pacific_allowed(self):
        moment = dt.datetime(2026, 6, 16, 17, 0, tzinfo=dt.timezone.utc)
        result = check_calling_window("CA", moment)
        assert result.allowed

    def test_monday_never_allowed(self):
        moment = dt.datetime(2026, 6, 15, 17, 0, tzinfo=dt.timezone.utc)
        assert not check_calling_window("CA", moment).allowed

    def test_evening_never_allowed(self):
        moment = dt.datetime(2026, 6, 16, 4, 0, tzinfo=dt.timezone.utc)
        assert not check_calling_window("NY", moment).allowed

    def test_state_maps(self):
        assert timezone_for_state("or") == "America/Los_Angeles"
        assert timezone_for_state("zz") == "America/New_York"


class TestPlaceCall:
    def test_missing_phone_reported(self, monkeypatch):
        monkeypatch.setenv("FISH_API_KEY", "x")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test")
        monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
        monkeypatch.setenv("TWILIO_CALLBACK_BASE_URL", "https://example.test")
        result = place_call({"case_id": "RC-1"})
        assert not result.ok
        assert "phone" in result.error.lower()

    def test_missing_env_reported(self, monkeypatch):
        for key in ("FISH_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_CALLBACK_BASE_URL"):
            monkeypatch.delenv(key, raising=False)
        assert not place_call({"case_id": "RC-1", "customer_phone": "+15551112222"}).ok


class TestSpeech:
    def test_number_zero(self):
        assert number_to_words(0) == "zero"

    def test_number_thousands(self):
        assert number_to_words(12_500) == "twelve thousand five hundred"

    def test_amount_with_cents(self):
        assert amount_to_words(23_650.75) == (
            "twenty-three thousand six hundred fifty dollars and seventy-five cents"
        )

    def test_amount_whole(self):
        assert amount_to_words(100.0) == "one hundred dollars"

    def test_date_words(self):
        assert date_to_words("2026-05-08") == "May eighth, twenty twenty-six"

    def test_ordinal_words(self):
        assert ordinal_to_words(1) == "first"
        assert ordinal_to_words(21) == "twenty-first"
        assert ordinal_to_words(30) == "thirtieth"

class TestDynamicVars:
    def test_live_case_state_is_available_to_voice(self):
        from src.agents._case_state import update_case_state
        from src.voice.dynamic_vars import fetch_case_dynamic_vars

        update_case_state(
            "RC-live-voice",
            customer_first_name="Demo",
            invoice_no="INV-LIVE-1",
            amount_usd=125.0,
            outstanding_balance_usd=125.0,
        )

        values = fetch_case_dynamic_vars("RC-live-voice")
        assert values["customer_first_name"] == "Demo"
        assert values["invoice_id"] == "INV-LIVE-1"
        assert values["invoice_outstanding"] == "$125.00"
