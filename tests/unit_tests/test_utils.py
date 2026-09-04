#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for shared Sui/Walrus helpers in pytusk.core.ops.coins."""

import types

import pytest
from pysui import SuiRpcResult

from pytusk.core.ops.coins import assert_coin_usable, prepare_wal_coin_for_amount


class _FakeCoinClient:
    """Fake ``WalrusClient``-shaped object for ``assert_coin_usable``.

    Implements only ``execute(command=...)``, the subset the function under
    test uses. Every dispatched command is recorded in ``calls`` so the
    single-fetch expectation can be asserted.
    """

    def __init__(self, *, response: SuiRpcResult) -> None:
        self.response = response
        self.calls: list[object] = []

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the canned response."""
        self.calls.append(command)
        return self.response


def _coin(*, owner: str | None, balance: int | None) -> object:
    """Build a minimal fetched-object stub exposing owner and balance."""
    owner_msg = types.SimpleNamespace(address=owner) if owner is not None else None
    return types.SimpleNamespace(owner=owner_msg, balance=balance)


class TestAssertCoinUsable:
    """Owner membership and minimum balance preconditions."""

    async def test_accepts_owner_in_set(self) -> None:
        client = _FakeCoinClient(
            response=SuiRpcResult(True, "", _coin(owner="0xa", balance=100))
        )
        await assert_coin_usable(
            client=client,  # type: ignore[arg-type]
            coin_id="0xcoin",
            owners={"0xa", "0xb"},
            minimum_balance=100,
        )
        assert len(client.calls) == 1

    async def test_accepts_sponsor_as_owner(self) -> None:
        client = _FakeCoinClient(
            response=SuiRpcResult(True, "", _coin(owner="0xb", balance=500))
        )
        await assert_coin_usable(
            client=client,  # type: ignore[arg-type]
            coin_id="0xcoin",
            owners={"0xa", "0xb"},
            minimum_balance=100,
        )

    async def test_balance_exactly_minimum_is_accepted(self) -> None:
        client = _FakeCoinClient(
            response=SuiRpcResult(True, "", _coin(owner="0xa", balance=100))
        )
        await assert_coin_usable(
            client=client,  # type: ignore[arg-type]
            coin_id="0xcoin",
            owners={"0xa"},
            minimum_balance=100,
        )

    async def test_rejects_owner_outside_set(self) -> None:
        client = _FakeCoinClient(
            response=SuiRpcResult(True, "", _coin(owner="0xc", balance=100))
        )
        with pytest.raises(RuntimeError, match="not by any of"):
            await assert_coin_usable(
                client=client,  # type: ignore[arg-type]
                coin_id="0xcoin",
                owners={"0xa", "0xb"},
                minimum_balance=100,
            )

    async def test_rejects_balance_below_minimum(self) -> None:
        client = _FakeCoinClient(
            response=SuiRpcResult(True, "", _coin(owner="0xa", balance=99))
        )
        with pytest.raises(RuntimeError, match="below the required"):
            await assert_coin_usable(
                client=client,  # type: ignore[arg-type]
                coin_id="0xcoin",
                owners={"0xa"},
                minimum_balance=100,
            )

    async def test_rejects_missing_owner(self) -> None:
        client = _FakeCoinClient(
            response=SuiRpcResult(True, "", _coin(owner=None, balance=100))
        )
        with pytest.raises(RuntimeError, match="reports no owner"):
            await assert_coin_usable(
                client=client,  # type: ignore[arg-type]
                coin_id="0xcoin",
                owners={"0xa"},
                minimum_balance=100,
            )

    async def test_rejects_object_without_balance(self) -> None:
        client = _FakeCoinClient(
            response=SuiRpcResult(True, "", _coin(owner="0xa", balance=None))
        )
        with pytest.raises(RuntimeError, match="reports no balance"):
            await assert_coin_usable(
                client=client,  # type: ignore[arg-type]
                coin_id="0xcoin",
                owners={"0xa"},
                minimum_balance=100,
            )

    async def test_rejects_failed_fetch(self) -> None:
        client = _FakeCoinClient(response=SuiRpcResult(False, "boom", None))
        with pytest.raises(RuntimeError, match="Cannot fetch coin"):
            await assert_coin_usable(
                client=client,  # type: ignore[arg-type]
                coin_id="0xcoin",
                owners={"0xa"},
                minimum_balance=100,
            )


class _FakeCoinListClient:
    """Fake ``WalrusClient``-shaped object for ``prepare_wal_coin_for_amount``.

    Implements ``execute`` (serving ``GetCoinMetaData``) and
    ``execute_for_all`` (serving ``GetAddressCoinBalances``/``GetCoins``),
    each returning canned responses in call order, plus
    ``.config.network.wal_coin_type``.
    """

    def __init__(
        self,
        *,
        execute_responses: list[SuiRpcResult],
        execute_for_all_responses: list[SuiRpcResult],
        wal_coin_type: str = "0x2::wal::WAL",
    ) -> None:
        self._execute_responses = list(execute_responses)
        self._execute_for_all_responses = list(execute_for_all_responses)
        self.config = types.SimpleNamespace(
            network=types.SimpleNamespace(wal_coin_type=wal_coin_type)
        )
        self.executed: list[object] = []
        self.execute_for_all_calls: list[object] = []

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the next canned response."""
        self.executed.append(command)
        return self._execute_responses.pop(0)

    async def execute_for_all(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the next canned response."""
        self.execute_for_all_calls.append(command)
        return self._execute_for_all_responses.pop(0)


class _RecordingCoinTxn:
    """Minimal recording fake for ``split_coin``/``merge_coins`` ordering."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def split_coin(self, *, coin: object, amounts: list[object]) -> str:
        """Record the split and return a sentinel result."""
        self.calls.append(("split_coin", {"coin": coin, "amounts": amounts}))
        return "split-result"

    async def merge_coins(self, *, merge_to: object, merge_from: list[object]) -> None:
        """Record the merge. ``merge_coins``'s own result is not chainable."""
        self.calls.append(
            ("merge_coins", {"merge_to": merge_to, "merge_from": merge_from})
        )


def _balances_result(*, coin_type: str = "0x2::wal::WAL") -> SuiRpcResult:
    """A ``GetAddressCoinBalances`` result carrying a single WAL entry."""
    return SuiRpcResult(
        True,
        "",
        types.SimpleNamespace(balances=[types.SimpleNamespace(coin_type=coin_type)]),
    )


def _coin_metadata_result(*, decimals: int = 9) -> SuiRpcResult:
    """A ``GetCoinMetaData`` result carrying only ``decimals``."""
    return SuiRpcResult(
        True, "", types.SimpleNamespace(metadata=types.SimpleNamespace(decimals=decimals))
    )


def _coins_result(*, coins: list[tuple[str, int]]) -> SuiRpcResult:
    """A ``GetCoins`` result carrying the given (object_id, balance) pairs."""
    objects = [
        types.SimpleNamespace(object_id=object_id, balance=balance)
        for object_id, balance in coins
    ]
    return SuiRpcResult(True, "", types.SimpleNamespace(objects=objects))


class TestPrepareWalCoinForAmount:
    """The exact-match / split / merge-then-split coin-selection algorithm."""

    async def test_exact_match_returns_object_id_no_ptb_command(self) -> None:
        """An owned coin whose balance equals amount exactly needs no PTB command."""
        client = _FakeCoinListClient(
            execute_responses=[_coin_metadata_result()],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xexact", 1000), ("0xother", 5000)]),
            ],
        )
        txn = _RecordingCoinTxn()

        result = await prepare_wal_coin_for_amount(
            txn=txn,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            owner="0xsender",
            amount=1000,
        )

        assert result == "0xexact"
        assert txn.calls == []

    async def test_single_coin_split_when_largest_exceeds_amount(self) -> None:
        """The largest owned coin covers amount alone: split off exactly amount."""
        client = _FakeCoinListClient(
            execute_responses=[_coin_metadata_result()],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xbig", 5000)]),
            ],
        )
        txn = _RecordingCoinTxn()

        result = await prepare_wal_coin_for_amount(
            txn=txn,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            owner="0xsender",
            amount=1000,
        )

        assert result == "split-result"
        assert txn.calls == [("split_coin", {"coin": "0xbig", "amounts": [1000]})]

    async def test_merge_then_split_when_no_single_coin_covers(self) -> None:
        """No single coin covers amount: merge into the largest, then split."""
        client = _FakeCoinListClient(
            execute_responses=[_coin_metadata_result()],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xprimary", 600), ("0xsecond", 500)]),
            ],
        )
        txn = _RecordingCoinTxn()

        result = await prepare_wal_coin_for_amount(
            txn=txn,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            owner="0xsender",
            amount=1000,
        )

        assert result == "split-result"
        assert txn.calls == [
            ("merge_coins", {"merge_to": "0xprimary", "merge_from": ["0xsecond"]}),
            ("split_coin", {"coin": "0xprimary", "amounts": [1000]}),
        ]

    async def test_insufficient_total_balance_raises(self) -> None:
        """An owner whose total WAL balance falls short raises, no PTB command added."""
        client = _FakeCoinListClient(
            execute_responses=[_coin_metadata_result()],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xonly", 100)]),
            ],
        )
        with pytest.raises(RuntimeError, match="less than the requested"):
            await prepare_wal_coin_for_amount(
                txn=_RecordingCoinTxn(),  # type: ignore[arg-type]
                client=client,  # type: ignore[arg-type]
                owner="0xsender",
                amount=1000,
            )

    async def test_no_wal_coins_raises(self) -> None:
        """No owned WAL coin objects at all raises."""
        client = _FakeCoinListClient(
            execute_responses=[_coin_metadata_result()],
            execute_for_all_responses=[_balances_result(), _coins_result(coins=[])],
        )
        with pytest.raises(RuntimeError, match="No WAL coin objects found"):
            await prepare_wal_coin_for_amount(
                txn=_RecordingCoinTxn(),  # type: ignore[arg-type]
                client=client,  # type: ignore[arg-type]
                owner="0xsender",
                amount=1000,
            )
