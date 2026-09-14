from __future__ import annotations

import pytest

from src.concierge.audit_logger import (
    AuditLogger,
    MissingRequiredFieldError,
    UnknownEventTypeError,
)


class TestAuditLogger:
    def _logger(self, tmp_path):
        return AuditLogger(path=tmp_path / "audit.jsonl")

    def test_write_returns_timestamped_record(self, tmp_path):
        logger = self._logger(tmp_path)
        record = logger.write(
            "request",
            request_id="req-1",
            case_id="RC-1",
            requesting_agent="diplomat",
            action_type="send_email",
        )
        assert record["type"] == "request"
        assert "ts" in record

    def test_rejects_unknown_event(self, tmp_path):
        with pytest.raises(UnknownEventTypeError):
            self._logger(tmp_path).write("mystery_event")

    def test_rejects_missing_required_field(self, tmp_path):
        with pytest.raises(MissingRequiredFieldError):
            self._logger(tmp_path).write("request", request_id="req-1")

    def test_tail_returns_recent(self, tmp_path):
        logger = self._logger(tmp_path)
        for tag in ("a", "b", "c"):
            logger.write(
                "request",
                request_id=tag,
                case_id="RC-1",
                requesting_agent="diplomat",
                action_type="x",
            )
        recent = logger.tail(2)
        assert [entry["request_id"] for entry in recent] == ["b", "c"]

    def test_count_by_type(self, tmp_path):
        logger = self._logger(tmp_path)
        logger.write(
            "request",
            request_id="a",
            case_id="RC-1",
            requesting_agent="diplomat",
            action_type="x",
        )
        logger.write("response", request_id="a", selected="approve")
        counts = logger.count_by_type()
        assert counts == {"request": 1, "response": 1}

    def test_for_case_filters(self, tmp_path):
        logger = self._logger(tmp_path)
        logger.write(
            "request",
            request_id="a",
            case_id="RC-1",
            requesting_agent="diplomat",
            action_type="x",
        )
        logger.write(
            "request",
            request_id="b",
            case_id="RC-2",
            requesting_agent="diplomat",
            action_type="x",
        )
        assert len(logger.for_case("RC-1")) == 1
