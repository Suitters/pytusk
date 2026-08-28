#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for shared Sui/Walrus helpers in pytusk.core.ops.coins."""

import types

import pytest
from pysui import SuiRpcResult

from pytusk.core.ops.coins import assert_coin_usable


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
