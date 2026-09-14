from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from src.payments.hedera_client import (
    HederaClient,
    HederaError,
    HederaTransfer,
    USDC_TOKEN_IDS,
    is_configured,
    mirror_node_url,
    network,
    transaction_explorer_url,
    usdc_token_id,
    validate_account_id,
)
from src.payments.usdc_display import UsdcDepositProvider
from src.payments.wallet_poller import (
    apply_transfer,
    case_id_from_transfer,
    poll_once,
    PollerState,
)


class TestAccountId:
    def test_accepts_shard_realm_num(self):
        assert validate_account_id("0.0.12345") == "0.0.12345"

    def test_strips_whitespace(self):
        assert validate_account_id("  0.0.1  ") == "0.0.1"

    @pytest.mark.parametrize("bad", ["0x1234", "abc", "0.0", "", "not-an-id"])
    def test_rejects_non_hedera(self, bad):
        with pytest.raises(HederaError):
            validate_account_id(bad)


class TestNetwork:
    def test_default_network(self, monkeypatch):
        monkeypatch.delenv("HEDERA_NETWORK", raising=False)
        assert network() == "testnet"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HEDERA_NETWORK", "MAINNET")
        assert network() == "mainnet"

    def test_mirror_node_for_testnet(self, monkeypatch):
        monkeypatch.setenv("HEDERA_NETWORK", "testnet")
        monkeypatch.delenv("HEDERA_MIRROR_NODE", raising=False)
        assert "testnet" in mirror_node_url()

    def test_usdc_token_defaults_by_network(self, monkeypatch):
        monkeypatch.setenv("HEDERA_NETWORK", "mainnet")
        monkeypatch.delenv("HEDERA_USDC_TOKEN_ID", raising=False)
        assert usdc_token_id() == USDC_TOKEN_IDS["mainnet"]

    def test_transaction_explorer_url_uses_selected_network(self):
        assert transaction_explorer_url("0.0.5000@1234.567", "testnet") == (
            "https://hashscan.io/testnet/transaction/0.0.5000@1234.567"
        )

    def test_transfer_metadata_includes_an_explorer_url(self):
        transfer = HederaTransfer(
            transaction_id="0.0.5000@1234.567",
            consensus_ts="1234.567",
            amount_usdc=Decimal("1"),
            sender_account_id="0.0.9",
            memo="recoverly:RC-2026-0042",
        )
        assert transfer.as_dict()["explorer_url"].startswith(
            "https://hashscan.io/testnet/transaction/"
        )

    def test_configured_requires_operator(self, monkeypatch):
        monkeypatch.delenv("HEDERA_OPERATOR_ID", raising=False)
        monkeypatch.delenv("HEDERA_OPERATOR_KEY", raising=False)
        assert not is_configured()


class TestUsdcDeposit:
    @pytest.mark.asyncio
    async def test_metadata_surfaces_hedera_fields(self, monkeypatch):
        monkeypatch.setenv("HEDERA_RECEIVING_ACCOUNT_ID", "0.0.5000")
        monkeypatch.setenv("HEDERA_USDC_TOKEN_ID", "0.0.429274")
        monkeypatch.setenv("HEDERA_NETWORK", "testnet")

        provider = UsdcDepositProvider()
        result = await provider.create_payment(
            Decimal("23650"), "USDC", {"case_id": "RC-2026-0042"}
        )
        assert result.metadata["display_type"] == "hedera_usdc"
        assert result.metadata["receiving_account_id"] == "0.0.5000"
        assert result.metadata["token_id"] == "0.0.429274"
        assert result.metadata["network"] == "testnet"
        assert result.metadata["memo"] == "recoverly:RC-2026-0042"
        assert result.metadata["settlement_detection"] == "mirror_node_poll"


class TestWalletPoller:
    def test_case_id_from_memo(self):
        transfer = HederaTransfer(
            transaction_id="0.0.42@1234.5",
            consensus_ts="1234.5",
            amount_usdc=Decimal("8750"),
            sender_account_id="0.0.9",
            memo="recoverly:RC-2026-0042",
        )
        assert case_id_from_transfer(transfer) == "RC-2026-0042"

    def test_case_id_from_wallet_ledger(self, monkeypatch, tmp_path):
        from src.payments import wallet_ledger

        ledger = tmp_path / "ledger.json"
        ledger.write_text('{"0.0.9": "RC-2026-0099"}', encoding="utf-8")
        monkeypatch.setattr(wallet_ledger, "LEDGER_PATH", ledger)

        transfer = HederaTransfer(
            transaction_id="tx1",
            consensus_ts="1.0",
            amount_usdc=Decimal("1"),
            sender_account_id="0.0.9",
            memo="",
        )
        assert case_id_from_transfer(transfer) == "RC-2026-0099"

    def test_apply_transfer_deduplicates(self, isolated_data, tmp_path, monkeypatch):
        from src.payments import dedup

        monkeypatch.setattr(dedup, "DEFAULT_PATH", tmp_path / "processed.json")
        transfer = HederaTransfer(
            transaction_id="tx-dupe",
            consensus_ts="1.0",
            amount_usdc=Decimal("100"),
            sender_account_id="0.0.9",
            memo="recoverly:RC-1",
        )

        first = apply_transfer(transfer)
        second = apply_transfer(transfer)
        assert first.get("deduplicated") in (None, False)
        assert second["deduplicated"] is True

    def test_poll_once_needs_receiving_account(self, isolated_data, monkeypatch):
        monkeypatch.delenv("HEDERA_RECEIVING_ACCOUNT_ID", raising=False)
        result = poll_once(HederaClient(), PollerState())
        assert not result["ok"]
        assert "HEDERA_RECEIVING_ACCOUNT_ID" in result["reason"]

    def test_poll_once_processes_new_transfers(self, isolated_data, monkeypatch, tmp_path):
        from src.payments import dedup

        monkeypatch.setattr(dedup, "DEFAULT_PATH", tmp_path / "processed.json")
        monkeypatch.setenv("HEDERA_RECEIVING_ACCOUNT_ID", "0.0.5000")

        transfer = HederaTransfer(
            transaction_id="tx-new",
            consensus_ts="99.9",
            amount_usdc=Decimal("100"),
            sender_account_id="0.0.9",
            memo="recoverly:RC-2026-0042",
        )
        client = HederaClient()
        with patch.object(client, "recent_inbound_transfers", return_value=[transfer]):
            state = PollerState()
            outcome = poll_once(client, state)

        assert outcome["ok"] and outcome["changed"]
        assert state.seen_transaction_ids == ["tx-new"]
        assert state.last_consensus_ts == "99.9"
