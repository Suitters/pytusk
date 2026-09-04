#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for relay tip PTB composition and standalone payment."""

import types
from typing import Any

import pytest
from pysui import SuiRpcResult
from pysui.sui.sui_common.txn_pure import PureInput
from pysui.sui.sui_common.txn_transaction_builder import ProgrammableTransactionBuilder

from pytusk.core.relay_upload import tip as tip_module
from pytusk.core.relay_upload.tip import (
    FROM_GAS,
    add_tip,
    build_auth_package,
    execute_tip,
)
from pytusk.core.types import TipPaymentError

_RELAY_ADDRESS = "0xrelay"
_GAS_SENTINEL = "GAS-SENTINEL"


class _FakeTxn:
    """Transaction stub carrying a REAL ProgrammableTransactionBuilder.

    The builder is genuine so the auth package's registration, its index,
    and its exact byte payload are exercised rather than mocked. Only the
    command helpers are stubbed, to record their arguments.
    """

    def __init__(self) -> None:
        self.builder = ProgrammableTransactionBuilder()
        self.gas = _GAS_SENTINEL
        self.splits: list[tuple[Any, list[int]]] = []
        self.transfers: list[tuple[list[Any], str]] = []

    async def split_coin(self, *, coin: Any, amounts: list[int]) -> str:
        """Record the split and return a placeholder result argument."""
        self.splits.append((coin, amounts))
        return "split-result"

    async def transfer_objects(self, *, transfers: list[Any], recipient: str) -> None:
        """Record the transfer."""
        self.transfers.append((transfers, recipient))

    async def build_and_sign(self) -> dict:
        """Return a placeholder signed-transaction dict."""
        return {"tx_bytes": "AAA", "signatures": []}


def _ok_result() -> SuiRpcResult:
    """A successful ExecuteTransaction result with usable effects."""
    return SuiRpcResult(
        True,
        "",
        types.SimpleNamespace(
            digest="0xdigest",
            effects=types.SimpleNamespace(
                status=types.SimpleNamespace(success=True, error=None)
            ),
        ),
    )


class _FakeExecClient:
    """Client stub for execute_tip."""

    def __init__(self, *, txn: _FakeTxn, result: SuiRpcResult) -> None:
        self._txn = txn
        self._result = result
        self.txn_kwargs: dict = {}
        self.pysui_client = types.SimpleNamespace(
            config=types.SimpleNamespace(active_address="0xsender")
        )

    async def transaction(self, **kwargs: Any) -> _FakeTxn:
        """Record how the transaction was opened and return the stub."""
        self.txn_kwargs = kwargs
        return self._txn

    async def execute(self, *, command: Any, **kwargs: Any) -> SuiRpcResult:
        """Return the canned execution result."""
        return self._result


def _inputs(txn: _FakeTxn) -> list:
    """Return the builder's registered CallArgs in insertion order."""
    return list(txn.builder.inputs.values())


class TestAddTipComposition:
    """The auth package must be input zero, byte-exact."""

    async def test_auth_package_is_input_zero(self) -> None:
        txn = _FakeTxn()
        package = build_auth_package(data=b"payload")
        await add_tip(
            txn=txn,
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=package,
            payment_coin=FROM_GAS,
        )
        first = _inputs(txn)[0]
        assert first.enum_name == "Pure"
        assert first.value == list(package.bcs)

    async def test_auth_package_payload_is_seventy_two_bytes(self) -> None:
        txn = _FakeTxn()
        package = build_auth_package(data=b"payload")
        await add_tip(
            txn=txn,
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=package,
            payment_coin=FROM_GAS,
        )
        assert len(_inputs(txn)[0].value) == 72

    async def test_from_gas_splits_from_the_gas_coin(self) -> None:
        txn = _FakeTxn()
        await add_tip(
            txn=txn,
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=build_auth_package(data=b"payload"),
            payment_coin=FROM_GAS,
        )
        assert txn.splits == [(_GAS_SENTINEL, [1000])]

    async def test_explicit_coin_splits_from_that_coin(self) -> None:
        txn = _FakeTxn()
        await add_tip(
            txn=txn,
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=build_auth_package(data=b"payload"),
            payment_coin="0xcoin",
        )
        assert txn.splits == [("0xcoin", [1000])]

    async def test_split_is_transferred_to_the_relay(self) -> None:
        txn = _FakeTxn()
        await add_tip(
            txn=txn,
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=build_auth_package(data=b"payload"),
        )
        assert txn.transfers == [(["split-result"], _RELAY_ADDRESS)]


class TestAddTipGuard:
    """Composing onto a non-empty transaction is refused before any spend."""

    async def test_rejects_transaction_with_existing_input(self) -> None:
        txn = _FakeTxn()
        txn.builder.input_pure(PureInput.as_input(b"already here"))
        with pytest.raises(TipPaymentError, match="must be the first composition"):
            await add_tip(
                txn=txn,
                relay_address=_RELAY_ADDRESS,
                tip_amount=1000,
                auth_package=build_auth_package(data=b"payload"),
            )

    async def test_rejects_transaction_with_existing_command(self) -> None:
        txn = _FakeTxn()
        txn.builder.commands.append(object())
        with pytest.raises(TipPaymentError, match="must be the first composition"):
            await add_tip(
                txn=txn,
                relay_address=_RELAY_ADDRESS,
                tip_amount=1000,
                auth_package=build_auth_package(data=b"payload"),
            )

    async def test_guard_error_names_the_stage(self) -> None:
        txn = _FakeTxn()
        txn.builder.input_pure(PureInput.as_input(b"already here"))
        with pytest.raises(TipPaymentError) as excinfo:
            await add_tip(
                txn=txn,
                relay_address=_RELAY_ADDRESS,
                tip_amount=1000,
                auth_package=build_auth_package(data=b"payload"),
            )
        assert excinfo.value.stage == "add_tip"

    async def test_guard_adds_nothing_to_the_transaction(self) -> None:
        txn = _FakeTxn()
        txn.builder.commands.append(object())
        with pytest.raises(TipPaymentError):
            await add_tip(
                txn=txn,
                relay_address=_RELAY_ADDRESS,
                tip_amount=1000,
                auth_package=build_auth_package(data=b"payload"),
            )
        assert txn.splits == []
        assert txn.transfers == []


class TestExecuteTip:
    """The standalone tip transaction returns resumption tokens."""

    async def test_returns_digest_and_nonce(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tip_module, "ExecuteTransaction", lambda **kwargs: object()
        )
        txn = _FakeTxn()
        client = _FakeExecClient(txn=txn, result=_ok_result())
        package = build_auth_package(data=b"payload")
        result = await execute_tip(
            client=client,  # type: ignore[arg-type]
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=package,
        )
        assert result.digest == "0xdigest"
        assert result.nonce == package.nonce_base64url
        assert result.relay_address == _RELAY_ADDRESS
        assert result.tip_amount == 1000

    async def test_defaults_sender_to_active_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tip_module, "ExecuteTransaction", lambda **kwargs: object()
        )
        txn = _FakeTxn()
        client = _FakeExecClient(txn=txn, result=_ok_result())
        await execute_tip(
            client=client,  # type: ignore[arg-type]
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=build_auth_package(data=b"payload"),
        )
        assert client.txn_kwargs["initial_sender"] == "0xsender"
        assert client.txn_kwargs["initial_sponsor"] is None

    async def test_passes_sponsor_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tip_module, "ExecuteTransaction", lambda **kwargs: object()
        )
        txn = _FakeTxn()
        client = _FakeExecClient(txn=txn, result=_ok_result())
        await execute_tip(
            client=client,  # type: ignore[arg-type]
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=build_auth_package(data=b"payload"),
            sender="0xother",
            sponsor="0xsponsor",
        )
        assert client.txn_kwargs["initial_sender"] == "0xother"
        assert client.txn_kwargs["initial_sponsor"] == "0xsponsor"

    async def test_composes_auth_package_into_the_transaction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tip_module, "ExecuteTransaction", lambda **kwargs: object()
        )
        txn = _FakeTxn()
        client = _FakeExecClient(txn=txn, result=_ok_result())
        package = build_auth_package(data=b"payload")
        await execute_tip(
            client=client,  # type: ignore[arg-type]
            relay_address=_RELAY_ADDRESS,
            tip_amount=1000,
            auth_package=package,
        )
        assert _inputs(txn)[0].value == list(package.bcs)

    async def test_submission_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tip_module, "ExecuteTransaction", lambda **kwargs: object()
        )
        txn = _FakeTxn()
        client = _FakeExecClient(
            txn=txn, result=SuiRpcResult(False, "boom", None)
        )
        with pytest.raises(RuntimeError, match="Tip transaction failed"):
            await execute_tip(
                client=client,  # type: ignore[arg-type]
                relay_address=_RELAY_ADDRESS,
                tip_amount=1000,
                auth_package=build_auth_package(data=b"payload"),
            )
