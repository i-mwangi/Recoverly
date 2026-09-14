from __future__ import annotations

from src.agents.base import StrandsAgentAdapter, build_mention_handles


def test_self_mentions_are_not_reply_targets():
    participants = [{"id": "agent-1", "handle": "mwangihenry336/investigator"}]
    assert build_mention_handles(
        "@investigator completed the review.",
        participants,
        self_handle="mwangihenry336/investigator",
        self_id="agent-1",
    ) == []


def test_adapter_uses_recovery_role_identity():
    adapter = StrandsAgentAdapter()
    assert adapter.handle == "base"
    assert adapter.agent_id == "recoverly-base"

def test_short_agent_mention_resolves_to_full_agent_handle():
    participants = [
        {"id": "agent-diplomat", "handle": "mwangihenry336/diplomat", "name": "Diplomat"},
        {"id": "user-1", "handle": "mwangihenry336", "name": "harry cravis"},
    ]
    assert build_mention_handles(
        "@diplomat Please draft this case.",
        participants,
        self_handle="mwangihenry336/investigator",
        self_id="agent-investigator",
    ) == ["mwangihenry336/diplomat"]


def test_role_alias_resolves_renamed_agent_handle():
    participants = [{"handle": "mwangihenry336/concierge-v2"}]
    assert build_mention_handles(
        "@concierge Please create the approval card.",
        participants,
        self_handle="mwangihenry336/tone-coach",
        self_id="agent-tone",
    ) == ["mwangihenry336/concierge-v2"]
