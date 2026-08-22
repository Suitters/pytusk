#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared Sui/Walrus helpers used across pytusk's on-chain modules.

These are the small, reusable primitives that sit underneath the PTB layers
in :mod:`pytusk.core.system_ops` and :mod:`pytusk.core.storage_ops`, and are
also called directly by pytusk's tusky command modules (e.g.
``tusky_cmds_lifecycle``, ``tusky_cmds_native_upload``). They were factored
out of ``system_ops`` so that a module composing storage-object transactions
does not have to import from the native-upload module to reach them.

Nothing here composes or submits a transaction. The helpers fall into three
groups:

- Resolution: :func:`resolve_package_id` reads the live Walrus package ID
  from the configured System object; :func:`select_wal_payment_coin` picks an
  owned ``Coin<WAL>`` to pay with.
- Effects inspection: :func:`require_success` checks a transaction's on-chain
  status and returns its effects; :func:`find_created_object_id` locates an
  object minted by a PTB.
- Finality: :func:`wait_for_finality` polls until a digest is visible in a
  checkpoint, with :data:`DEFAULT_FINALITY_MAX_ATTEMPTS` and
  :data:`DEFAULT_FINALITY_MAX_DELAY` as its default budget.
"""

from __future__ import annotations

import asyncio

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import GetAddressCoinBalances, GetCoins, GetObject, GetTransaction

from pytusk.client.walrus_client import WalrusClient

__all__ = [
    "DEFAULT_FINALITY_MAX_ATTEMPTS",
    "DEFAULT_FINALITY_MAX_DELAY",
    "find_created_object_id",
    "require_success",
    "resolve_package_id",
    "select_wal_payment_coin",
    "wait_for_finality",
]

DEFAULT_FINALITY_MAX_ATTEMPTS: int = 10
"""Fallback poll count for :func:`wait_for_finality`.

Used when a caller does not pass ``finality_max_attempts``.
"""

DEFAULT_FINALITY_MAX_DELAY: float = 1.0
"""Fallback per-attempt delay ceiling, in seconds, for :func:`wait_for_finality`.

Backoff doubles from an eighth of this value and is clamped here, so the
default budget is roughly 7 seconds across
:data:`DEFAULT_FINALITY_MAX_ATTEMPTS` attempts.
"""


async def resolve_package_id(*, client: WalrusClient, system_object: str) -> str:
    """Read the current Walrus package ID from the configured System object.

    ``pytusk.tusky.tusky_cmds_common``'s ``_walrus_package_id`` delegates to
    this function directly; the same pattern is also used by
    ``_ensure_wal``/``_cleanup_blobs`` in
    ``tests/integration_tests/conftest.py``: the package ID is read from
    ``System.package_id`` rather than assumed from a Blob's type-tag address,
    since the type-tag address can go stale after a package upgrade.

    Args:
        client (WalrusClient): Client used to fetch the System object.
        system_object (str): Object ID of the configured Walrus System object.

    Returns:
        str: The current Walrus package ID.

    Raises:
        RuntimeError: If the System object cannot be fetched.
    """
    result = await client.execute(command=GetObject(object_id=system_object))
    if not result.is_ok():
        raise RuntimeError(
            f"Cannot fetch System object {system_object}: {result.result_string}"
        )
    return result.result_data.json.struct_value.fields["package_id"].string_value


def _matches_wal_coin_type(*, coin_type: str, wal_coin_type: str) -> bool:
    """Check whether a coin_type string identifies the WAL coin.

    The single implementation. ``pytusk.tusky.tusky_cmds`` carried a
    duplicate of this helper until it was consolidated here: the one-way
    dependency rule forbids ``core`` importing ``tusky``, not ``tusky``
    importing ``core``, and ``tusky`` already imports from ``pytusk``
    freely. Uses an exact match against ``wal_coin_type`` when the active
    network has a pinned value (currently mainnet only); falls back to a
    substring match when unpinned (e.g. testnet, whose contracts are
    redeployed and don't have a stable package address to pin against).

    Args:
        coin_type (str): The coin_type string to check.
        wal_coin_type (str): The active network's pinned WAL coin type, or
            "" if unpinned.

    Returns:
        bool: True if coin_type identifies the WAL coin.
    """
    if wal_coin_type:
        return coin_type == wal_coin_type
    return "::wal::WAL" in coin_type


async def select_wal_payment_coin(*, client: WalrusClient, owner: str) -> str:
    """Select a single owned WAL coin object to use as ``Coin<WAL>`` payment.

    Adapts the coin-selection block used by ``extend_blob_expiration`` in
    ``pytusk.tusky.tusky_cmds_lifecycle``: resolve the owner's WAL
    ``coin_type`` via a balance listing (matched via
    :func:`_matches_wal_coin_type`, the same pinned-exact/substring-fallback
    logic ``tusky_cmds_lifecycle`` uses), then list
    owned coins of that type via ``GetCoins`` and take the largest by
    balance. No merge logic is added -- ``reserve_space``/``register_blob``
    each deduct what they need from a single ``&mut Coin<WAL>`` and leave the
    remainder in place, matching how ``system::extend_blob`` payment already
    works.

    This is a FALLBACK ONLY: an SDK developer composing Tx1 directly via
    :func:`~pytusk.core.system_ops.add_reserve_and_register` supplies
    ``payment_coin`` themselves (they know their own wallet's WAL coin type
    without a network round trip). This helper exists so
    :func:`~pytusk.core.system_ops.execute_reserve_and_register` can still
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
            and _matches_wal_coin_type(
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


def find_created_object_id(
    *, effects: sui_prot.TransactionEffects, owner: str
) -> str:
    """Find the object ID of the single object created and owned by ``owner``.

    Public so a caller who now owns transaction submission (per the
    caller-owns-the-transaction model described in
    :mod:`pytusk.core.system_ops`) and holds their own ``TransactionEffects``
    can locate a PTB's created object without reimplementing this lookup.

    Reads ``TransactionEffects.changed_objects`` (verified directly against
    the installed pysui proto definitions: ``ChangedObject.id_operation`` and
    ``ChangedObject.output_owner.address``) rather than following an existing
    pytusk pattern, because no existing pytusk code path parses a PTB's
    created-object effects -- every other PTB builder in this repo (
    ``extend_blob_expiration``, ``delete_blob``, ``exchange_for_wal``) only
    needed to check ``.status``/``.gas_used``/``.balance_changes``, never an
    object ID minted by the PTB itself.

    UNVERIFIED against a live node -- see the deliverable-4 report. If this
    assumption is wrong, it fails loudly here (after the transaction has
    already succeeded on-chain) rather than corrupting a ``Registration``.

    Args:
        effects (sui_prot.TransactionEffects): Effects of a successfully
            executed transaction.
        owner (str): Address expected to own the newly created object.

    Returns:
        str: Object ID of the created object owned by ``owner``.

    Raises:
        RuntimeError: If no such object is found in ``effects``.
    """
    for change in effects.changed_objects or []:
        if (
            change.id_operation == sui_prot.ChangedObjectIdOperation.CREATED
            and change.output_owner
            and change.output_owner.address == owner
            and change.object_id
        ):
            return change.object_id
    raise RuntimeError(
        f"Could not find a newly created object owned by {owner} in the "
        "transaction effects."
    )


def require_success(
    *, result_data: object, label: str
) -> sui_prot.TransactionEffects:
    """Check an ExecuteTransaction result's effects.status and return the effects.

    Status is read from ``result_data.effects.status`` -- NOT
    ``result_data.transaction.effects.status`` -- per the confirmed pysui
    result shape (``tests/integration_tests/conftest.py``'s ``_ensure_wal``).

    Args:
        result_data (object): ``SuiRpcResult.result_data`` from a successful
            ``ExecuteTransaction`` call (i.e. ``result.is_ok()`` already True).
        label (str): Human-readable name of the transaction, used in errors.

    Returns:
        sui_prot.TransactionEffects: The transaction's effects.

    Raises:
        RuntimeError: If the transaction aborted on-chain.
    """
    effects = result_data.effects  # type: ignore[attr-defined]
    status = effects.status if effects else None
    if not (status and status.success):
        desc = status.error.description if status and status.error else "unknown error"
        raise RuntimeError(f"{label} transaction aborted on-chain: {desc}")
    return effects


async def wait_for_finality(
    *,
    client: WalrusClient,
    digest: str,
    max_attempts: int = DEFAULT_FINALITY_MAX_ATTEMPTS,
    max_delay: float = DEFAULT_FINALITY_MAX_DELAY,
) -> bool:
    """Poll ``GetTransaction`` until ``digest`` is visible in a checkpoint.

    A successful ``ExecuteTransaction`` means the transaction was accepted,
    NOT that its effects are readable yet. Reading a newly created object
    before the transaction lands in a checkpoint returns a stub -- an
    ``Object`` with no ``object_id`` and no JSON view -- from an RPC call
    that still reports ``is_ok()``. Callers that read back objects created
    by a transaction must wait on this function first, or they will
    misread that race as a malformed response.

    ``GetTransaction`` returns ``None`` while the digest is unfound, which
    is the poll signal. Delay starts at an eighth of ``max_delay`` and
    doubles per attempt, clamped at ``max_delay``.

    Args:
        client (WalrusClient): Client used to query the transaction.
        digest (str): Transaction digest to wait on, as returned by
            ``ExecuteTransaction``.
        max_attempts (int): Maximum number of polls before giving up.
            Defaults to :data:`DEFAULT_FINALITY_MAX_ATTEMPTS`.
        max_delay (float): Ceiling, in seconds, on the delay between
            polls. Defaults to :data:`DEFAULT_FINALITY_MAX_DELAY`.

    Returns:
        bool: ``True`` once the digest is visible, ``False`` if it was
            still not visible after ``max_attempts`` polls. A ``False``
            return is not proof the transaction failed -- the caller
            already knows it succeeded -- only that it had not become
            readable within the budget.
    """
    delay = max_delay / 8
    for attempt in range(max_attempts):
        result = await client.execute(command=GetTransaction(digest=digest))
        if (
            result.is_ok()
            and result.result_data is not None
            and result.result_data.checkpoint
        ):
            return True
        if attempt + 1 < max_attempts:
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
    return False
