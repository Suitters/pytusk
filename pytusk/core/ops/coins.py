#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Client-driven WAL coin selection and validation.

The three functions defined here each take a
:class:`~pytusk.client.walrus_client.WalrusClient` -- none compose or submit
a PTB, so they sit in :mod:`pytusk.core.ops` rather than a
``*_compose.py`` module. :func:`~pytusk.core.chain.coin_types.matches_wal_coin_type`
is re-exported here (imported, not defined) for callers within this module
that match a coin_type string against the pinned WAL coin type; it moved to
:mod:`pytusk.core.chain.coin_types` at Plan #28's architect review since it
is client-free and belongs below ``pytusk/client/``.
"""

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import GetAddressCoinBalances, GetCoinMetaData, GetCoins, GetObject

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.chain.coin_types import matches_wal_coin_type

__all__ = [
    "assert_coin_usable",
    "matches_wal_coin_type",
    "select_wal_payment_coin",
    "wal_balance_and_decimals",
]


async def wal_balance_and_decimals(
    *, client: WalrusClient, owner: str
) -> tuple[sui_prot.Balance, int]:
    """Discover the owner's WAL coin balance entry and WAL's decimal precision.

    Looks up the owner's coin balances to find the exact WAL coin type
    string, then fetches its on-chain CoinMetadata for the decimals value
    (not hardcoded, since it must not be assumed to match SUI's).

    Moved here from ``pytusk.tusky.tusky_cmds_common`` at Plan #28 step 10:
    matching a WAL coin type and reading its decimals is Walrus domain logic
    an SDK user wants, not CLI presentation, and it belongs beside
    :func:`matches_wal_coin_type`, which it calls. In doing so it traded the
    CLI's ``print``-and-``sys.exit(1)`` failure path for ``RuntimeError`` --
    a library function must not terminate its caller's process. That matches
    :func:`~pytusk.core.ops.system_reads.resolve_package_id`, whose failures
    the CLI already catches and renders itself. Nothing is spent by these
    reads, so raising discards no recoverable state.

    Args:
        client (WalrusClient): Client used to query balances and metadata.
        owner (str): Address whose balances are checked for a WAL coin type.

    Returns:
        tuple[sui_prot.Balance, int]: The owner's WAL balance entry
            (exposing coin_type, balance, coin_balance, address_balance)
            and WAL's decimals.

    Raises:
        RuntimeError: If the balance listing fails, the owner holds no WAL
            coin, the coin metadata read fails, or that metadata carries no
            decimals field.
    """
    balances_result = await client.execute_for_all(
        command=GetAddressCoinBalances(owner=owner)
    )
    if not balances_result.is_ok():
        raise RuntimeError(
            f"Error listing coin balances: {balances_result.result_string}"
        )
    wal_coin_type = client.config.network.wal_coin_type
    wal_entry = next(
        (
            entry
            for entry in balances_result.result_data.balances
            if entry.coin_type
            and matches_wal_coin_type(
                coin_type=entry.coin_type, wal_coin_type=wal_coin_type
            )
        ),
        None,
    )
    if wal_entry is None:
        raise RuntimeError(f"No WAL coins found for {owner}.")
    meta_result = await client.execute(
        command=GetCoinMetaData(coin_type=wal_entry.coin_type)
    )
    if not meta_result.is_ok():
        raise RuntimeError(
            f"Error fetching WAL coin metadata: {meta_result.result_string}"
        )
    metadata = meta_result.result_data.metadata
    if metadata is None or metadata.decimals is None:
        raise RuntimeError(
            f"WAL coin metadata for {wal_entry.coin_type} has no decimals field."
        )
    return wal_entry, metadata.decimals


async def select_wal_payment_coin(*, client: WalrusClient, owner: str) -> str:
    """Select a single owned WAL coin object to use as ``Coin<WAL>`` payment.

    Adapts the coin-selection block used by ``extend_blob_expiration`` in
    ``pytusk.tusky.tusky_cmds_lifecycle``: resolve the owner's WAL
    ``coin_type`` via a balance listing (matched via
    :func:`matches_wal_coin_type`, the same pinned-exact/substring-fallback
    logic ``tusky_cmds_lifecycle`` uses), then list
    owned coins of that type via ``GetCoins`` and take the largest by
    balance. No merge logic is added -- ``reserve_space``/``register_blob``
    each deduct what they need from a single ``&mut Coin<WAL>`` and leave the
    remainder in place, matching how ``system::extend_blob`` payment already
    works.

    This is a FALLBACK ONLY: an SDK developer composing Tx1 directly via
    :func:`~pytusk.core.ops.blob_compose.add_reserve_and_register` supplies
    ``payment_coin`` themselves (they know their own wallet's WAL coin type
    without a network round trip). This helper exists so
    :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register` can still
    select one automatically when ``payment_coin`` is omitted, for backward
    compatibility with callers of the old one-call behaviour.

    Args:
        client (WalrusClient): Client used to query balances and coins.
        owner (str): Address whose WAL coins are selected from.

    Returns:
        str: Object ID of the largest-balance WAL coin owned by ``owner``.

    Raises:
        RuntimeError: If balances or coins cannot be listed, or ``owner``
            owns no WAL coin.
    """
    balances_result = await client.execute_for_all(
        command=GetAddressCoinBalances(owner=owner)
    )
    if not balances_result.is_ok():
        raise RuntimeError(
            f"Cannot list coin balances for {owner}: {balances_result.result_string}"
        )
    wal_coin_type = client.config.network.wal_coin_type
    wal_entry = next(
        (
            entry
            for entry in balances_result.result_data.balances
            if entry.coin_type
            and matches_wal_coin_type(
                coin_type=entry.coin_type, wal_coin_type=wal_coin_type
            )
        ),
        None,
    )
    if wal_entry is None:
        raise RuntimeError(f"No WAL coins found for {owner}.")

    coins_result = await client.execute_for_all(
        command=GetCoins(owner=owner, coin_type=f"0x2::coin::Coin<{wal_entry.coin_type}>")
    )
    if not coins_result.is_ok():
        raise RuntimeError(
            f"Cannot list WAL coins for {owner}: {coins_result.result_string}"
        )
    coins = sorted(
        coins_result.result_data.objects, key=lambda c: c.balance or 0, reverse=True
    )
    if not coins:
        raise RuntimeError(f"No WAL coin objects found for {owner}.")
    return coins[0].object_id


async def assert_coin_usable(
    *, client: WalrusClient, coin_id: str, owners: set[str], minimum_balance: int
) -> None:
    """Assert a coin object is owned by one of ``owners`` and holds enough.

    Deliberately generic: it takes a SET of acceptable owners rather than
    sender/sponsor roles, so the same check serves a relay tip coin (which
    either party of a sponsored transaction may legitimately own) and the
    WAL payment coin passed to
    :func:`~pytusk.core.ops.blob_compose.add_reserve_and_register`.

    This is a caller-side precondition, so it RAISES rather than reporting
    an outcome -- named per the ``assert_...`` convention shared with
    :func:`~pytusk.core.native_upload.assert_certificate_epoch_current`.
    Checking BEFORE composing is the whole point: an unusable coin must
    fail before a transaction is built, not after money has moved.

    ``GetObject``'s default field mask already includes ``owner`` and
    ``balance``, so no explicit field mask is needed. ``Object.balance`` is
    the same field :func:`select_wal_payment_coin` already sorts on.
    ``Object.owner.address`` is UNVERIFIED against a live node -- no pytusk
    code path reads an owner off a fetched object today; the access path is
    taken from the proto type, which is the same ``Owner`` message that
    :func:`~pytusk.core.chain.effects.find_created_object_id` reads as
    ``output_owner.address``. If that is wrong it fails loudly here, before
    anything is spent.

    Args:
        client (WalrusClient): Client used to fetch the coin object.
        coin_id (str): Object ID of the coin to check.
        owners (set[str]): Addresses, any one of which may own the coin.
        minimum_balance (int): Smallest acceptable balance, in the coin's
            own base units.

    Raises:
        RuntimeError: If the coin cannot be fetched, reports no owner or no
            balance, is owned by an address outside ``owners``, or holds
            less than ``minimum_balance``.
    """
    result = await client.execute(command=GetObject(object_id=coin_id))
    if not result.is_ok():
        raise RuntimeError(f"Cannot fetch coin {coin_id}: {result.result_string}")
    obj = result.result_data
    owner = obj.owner.address if obj.owner else None
    if owner is None:
        raise RuntimeError(f"Coin {coin_id} reports no owner address.")
    if owner not in owners:
        raise RuntimeError(
            f"Coin {coin_id} is owned by {owner}, not by any of {sorted(owners)}."
        )
    if obj.balance is None:
        raise RuntimeError(f"Object {coin_id} reports no balance; it is not a coin.")
    if obj.balance < minimum_balance:
        raise RuntimeError(
            f"Coin {coin_id} balance {obj.balance} is below the required "
            f"{minimum_balance}."
        )
