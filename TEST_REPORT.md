# Recoverly verification

## Scope

The committed suite verifies the autonomous Strands tool boundary, case intake, document pairing, human decisions, outbound idempotency, payment routing, transfer reconciliation, signed webhooks, and notification handling.

The final local run completed with **302 passing tests** on Python 3.14.6. The targeted Ruff fatal-error rules and `pip check` also passed.

External network connections are blocked during tests. Email, Slack, voice, card, and wallet transports use controlled doubles, so verification does not contact buyers or move funds.

The autonomous-agent scenarios verify that routine 7–14 day reminders can execute, disputes are blocked from automatic delivery, repeated cycles cannot send twice, and judgment-heavy cases create only one operator decision request per day.

## Commands

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check src tests --select E9,F63,F7,F82
.venv/Scripts/python.exe -m pip check
```
