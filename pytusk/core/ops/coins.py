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
from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.chain.coin_types import matches_wal_coin_type

__all__ = [
    "assert_coin_usable",
    "matches_wal_coin_type",
    "prepare_wal_coin_for_amount",
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


async def prepare_wal_coin_for_amount(
    *, txn: AsyncSuiTransaction, client: WalrusClient, owner: str, amount: int
) -> str | bcs.Argument:
    """Ensure a ``Coin<WAL>`` worth exactly ``amount`` is available within ``txn``.

    Composes whatever PTB commands are needed to produce a coin argument
    holding exactly ``amount`` (in WAL's base units), for a Move call that
    consumes its ``Coin<WAL>`` argument BY VALUE (e.g.
    :func:`~pytusk.core.ops.shared_blob_compose.add_fund_shared_blob`'s
    ``shared_blob::fund``, whose ``added_funds: Coin<WAL>`` is not ``&mut``
    and is fully consumed) -- unlike :func:`select_wal_payment_coin`, whose
    caller passes the coin BY REFERENCE and needs no exact-amount coin at
    all.

    Algorithm, evaluated in order against ``owner``'s WAL coins sorted
    largest-balance-first:

    1. Pre-verify the owner's total WAL balance covers ``amount`` -- a
       caller-side precondition checked before any PTB command is added.
    2. If any single coin's balance equals ``amount`` exactly, return its
       object ID directly -- no PTB command is composed.
    3. Else if the largest coin's balance exceeds ``amount``, split
       ``amount`` off it via ``txn.split_coin`` and return the split
       result.
    4. Else, merge additional coins (largest-first) into the largest coin
       via ``txn.merge_coins`` until the merged balance covers ``amount``,
       then split ``amount`` off the MERGED COIN'S OBJECT ID (not
       ``merge_coins``'s own result, which pysui documents as not usable
       in a subsequent command) and return the split result.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add merge/split commands to, if needed.
        client (WalrusClient): Client used to query balances and coins.
        owner (str): Address whose WAL coins are selected from.
        amount (int): Exact amount the returned coin must hold, in WAL's
            base units (FROST).

    Returns:
        str | bcs.Argument: Object ID of an existing coin already holding
        exactly ``amount`` (no PTB command added), or the ``bcs.Argument``
        command result of a ``split_coin`` composed to produce it.

    Raises:
        RuntimeError: If balances or coins cannot be listed, or the
            owner's total WAL balance is less than ``amount``.
    """
    wal_entry, _decimals = await wal_balance_and_decimals(client=client, owner=owner)

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

    total_balance = sum(coin.balance or 0 for coin in coins)
    if total_balance < amount:
        raise RuntimeError(
            f"Owner {owner} holds {total_balance} FROST across all WAL "
            f"coins, less than the requested {amount}."
        )

    exact_match = next((coin for coin in coins if (coin.balance or 0) == amount), None)
    if exact_match is not None:
        return exact_match.object_id

    primary = coins[0]
    primary_balance = primary.balance or 0
    if primary_balance > amount:
        return await txn.split_coin(coin=primary.object_id, amounts=[amount])

    merge_from: list[str] = []
    running_balance = primary_balance
    for coin in coins[1:]:
        if running_balance >= amount:
            break
        merge_from.append(coin.object_id)
        running_balance += coin.balance or 0
    await txn.merge_coins(merge_to=primary.object_id, merge_from=merge_from)
    return await txn.split_coin(coin=primary.object_id, amounts=[amount])
