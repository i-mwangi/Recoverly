from __future__ import annotations

import datetime as dt

import pytest

from src.agents._utils import (
    detect_anomaly,
    detect_dispute,
    detect_extension_request,
    detect_partial_payment,
    detect_promise_to_pay,
    error_envelope,
    is_error_envelope,
    parse_json_object,
    safe_float,
)
from src.utils.sanitize import strip_slack_mentions
from src.utils.text import extract_code_block, slack_preview, truncate
from src.utils.timezone import (
    compute_pre_approval_deadline,
    customer_state_to_timezone,
    format_dual_timezone,
    is_business_day,
    is_us_holiday,
    is_within_business_hours,
    next_allowed_window,
)


class TestSafeFloat:
    def test_valid_number(self):
        assert safe_float("12.5") == 12.5

    def test_infinite_rejected(self):
        assert safe_float("inf") == 0.0

    def test_nan_rejected(self):
        assert safe_float("nan") == 0.0

    def test_invalid_returns_default(self):
        assert safe_float("nope", default=99.0) == 99.0


class TestJsonParsing:
    def test_extracts_from_fenced_block(self):
        text = 'noise ```json\n{"a": 1}\n``` after'
        assert parse_json_object(text) == {"a": 1}

    def test_handles_nested_objects(self):
        assert parse_json_object('{"outer": {"inner": 2}}') == {"outer": {"inner": 2}}

    def test_returns_none_for_junk(self):
        assert parse_json_object("nothing at all") is None

    def test_recovers_from_trailing_garbage(self):
        assert parse_json_object('{"a": 1} extra bytes') == {"a": 1}


class TestErrorEnvelope:
    def test_structured_envelope(self):
        import json as _json
        text = _json.dumps(error_envelope("preflight", "no fixture", "RC-1"))
        assert is_error_envelope(text)

    def test_legacy_marker(self):
        assert is_error_envelope("something failed \u26a0\ufe0f here")

    def test_marker_inside_fenced_block_ignored(self):
        assert not is_error_envelope("okay ```quote \u26a0\ufe0f```")

    def test_clean_text_not_flagged(self):
        assert not is_error_envelope("all good")


class TestIntentDetectors:
    def test_dispute(self):
        assert detect_dispute("we dispute the amount")
        assert not detect_dispute("thanks noted")

    def test_partial_payment(self):
        assert detect_partial_payment("can only pay $2,000 now")

    def test_extension(self):
        assert detect_extension_request("can we push out the payment by two weeks")

    def test_promise_to_pay(self):
        assert detect_promise_to_pay("we will wire the payment friday")

    def test_anomaly_welfare(self):
        assert detect_anomaly("i want to hurt myself") == "welfare"

    def test_anomaly_bankruptcy(self):
        assert detect_anomaly("we filed chapter 11 last month") == "bankruptcy"

    def test_anomaly_cease_and_desist(self):
        assert detect_anomaly("cease and desist immediately") == "cease_and_desist"

    def test_no_anomaly(self):
        assert detect_anomaly("everything is fine") is None


class TestSlackPreview:
    def test_short_text_untouched(self):
        assert slack_preview("hello") == "hello"

    def test_long_text_truncated(self):
        result = slack_preview("x" * 5_000)
        assert len(result) < 5_000
        assert "truncated" in result

    def test_extract_code_block(self):
        assert extract_code_block("prefix ```\nbody\n``` suffix") == "body"

    def test_extract_code_block_no_fence(self):
        assert extract_code_block("just text") == "just text"

    def test_truncate_short(self):
        assert truncate("hi", 100) == "hi"

    def test_truncate_long(self):
        assert truncate("abcdefgh", 5) == "abcd\u2026"


class TestSlackMentionScrub:
    @pytest.mark.parametrize(
        "raw",
        ["<!channel>", "<!here>", "<!everyone>", "&lt;!channel&gt;", "<@U0ABC>", "<#C0ABC|name>"],
    )
    def test_strips_documented_mentions(self, raw):
        assert "[mention-stripped]" in strip_slack_mentions(raw)

    def test_disarms_bare_at_signs(self):
        assert "\u200b" in strip_slack_mentions("hey @dana")

    def test_leaves_normal_text(self):
        assert strip_slack_mentions("please review").startswith("please review")


class TestTimezone:
    def test_state_to_timezone(self):
        assert customer_state_to_timezone("CA") == "America/Los_Angeles"
        assert customer_state_to_timezone("AZ") == "America/Phoenix"

    def test_unknown_state_falls_back(self):
        assert customer_state_to_timezone("ZZ") == "America/New_York"

    def test_july_4_2027_is_holiday(self):
        assert is_us_holiday(dt.date(2027, 7, 4))

    def test_business_day_excludes_weekend(self):
        assert not is_business_day(dt.date(2026, 6, 20), "email")

    def test_voice_business_day_only_tuesday_through_thursday(self):
        assert not is_business_day(dt.date(2026, 6, 15), "voice")
        assert is_business_day(dt.date(2026, 6, 16), "voice")

    def test_within_business_hours(self):
        pdt = dt.datetime(2026, 6, 16, 10, 0)
        assert is_within_business_hours(pdt, "voice")
        assert not is_within_business_hours(pdt.replace(hour=6), "voice")

    def test_next_email_slot_skips_weekend(self):
        friday_night = dt.datetime(2026, 6, 19, 23, 0, tzinfo=dt.timezone.utc)
        slot = next_allowed_window(
            channel="email",
            customer_timezone="America/New_York",
            not_before=friday_night,
        )
        assert slot.weekday() == 0

    def test_pre_approval_deadline_subtracts_lead(self):
        send_at = dt.datetime(2026, 6, 16, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
        deadline = compute_pre_approval_deadline(customer_local_send_at=send_at, channel="voice")
        assert (send_at - deadline.astimezone(send_at.tzinfo)).total_seconds() == 4 * 3600

    def test_format_dual_timezone(self):
        from zoneinfo import ZoneInfo
        send_at = dt.datetime(2026, 6, 16, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        result = format_dual_timezone(
            customer_local=send_at, customer_name="ABC", customer_state="CA"
        )
        assert result["customer_tz_short"] == "PT"
        assert "ABC" in result["customer_local_str"]
