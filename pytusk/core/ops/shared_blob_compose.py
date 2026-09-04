#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""COMPOSE layer for SharedBlob PTBs.

Contains :func:`add_share_blob`, :func:`add_fund_shared_blob`, and
:func:`add_extend_shared_blob` -- each PURE PTB COMPOSITION: they append one
move_call to a transaction the caller already created. None makes a network
call, builds, signs, or submits anything. Submission lives in
:mod:`pytusk.core.ops.shared_blob_execute`, whose ``execute_*`` wrappers open
a transaction, delegate all composition to the functions here, then build,
sign, and submit.

NO-CLIENT INVARIANT: nothing in this module imports or references a client /
transaction-executor type (e.g. :class:`~pytusk.client.walrus_client.WalrusClient`).
A compose function takes a ``txn: AsyncSuiTransaction`` and contributes to
it; it never submits.

Move entry points targeted (module ``walrus::shared_blob``, confirmed
against ``shared_blob.move``, none declared ``entry`` -- each returns unit
and is directly callable via ``move_call``)::

    new(blob: Blob, ctx: &mut TxContext)
    fund(self: &mut SharedBlob, added_funds: Coin<WAL>)
    extend(self: &mut SharedBlob, system: &mut System, extended_epochs: u32, ctx: &mut TxContext)

``new`` consumes ``blob`` by value and shares the newly created
``SharedBlob`` internally via ``transfer::share_object`` -- there is no PTB
result to consume or transfer, and no created-object ID known client-side
until the transaction executes; see
:func:`~pytusk.core.chain.effects.find_created_shared_object_id`. ``fund``
consumes ``added_funds`` by value (not ``&mut``), so the caller must supply a
coin argument holding EXACTLY the amount to deposit -- see
:func:`~pytusk.core.ops.coins.prepare_wal_coin_for_amount`. ``extend`` is its
own wrapper around ``system::extend_blob``, not a direct call to it --
``system::extend_blob`` takes ``blob: &mut Blob`` and has no ``SharedBlob``
overload; ``shared_blob::extend`` withdraws the ``SharedBlob``'s pooled
funds, calls ``system::extend_blob`` internally, then deposits back whatever
remains, so no payment coin is passed by the caller at all.

``new_funded`` (share-with-initial-funding in one call) is deliberately NOT
composed here -- backlog #20 scopes ``share_blob`` to object-id-only (no
``--amount``); funding a freshly shared blob is a separate
``fund_shared_blob`` call. ``new_funded`` is left for backlog #24's
``--share`` flag on ``store``/``store-quilt`` to compose directly if it
needs the atomic share-and-fund variant.
"""

from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

__all__ = [
    "add_extend_shared_blob",
    "add_fund_shared_blob",
    "add_share_blob",
]


async def add_share_blob(
    *, txn: AsyncSuiTransaction, package_id: str, blob_object: str
) -> None:
    """Add a single ``shared_blob::new`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created.

    Consumes ``blob_object`` BY VALUE -- the ``Blob`` must not be used
    again in the same transaction after this call. ``new`` shares the
    resulting ``SharedBlob`` internally; its new object ID is only known
    after execution (see the module docstring).

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to wrap into a new
            ``SharedBlob``. Move's ``blob: Blob`` argument (consumed).

    Returns:
        None.
    """
    await txn.move_call(
        target=f"{package_id}::shared_blob::new",
        arguments=[blob_object],
        type_arguments=[],
    )


async def add_fund_shared_blob(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    shared_blob_object: str,
    payment_coin: str | bcs.Argument,
) -> None:
    """Add a single ``shared_blob::fund`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created.

    ``fund`` consumes ``payment_coin`` BY VALUE -- its ENTIRE balance is
    deposited into the ``SharedBlob``'s pooled funds, unlike a ``&mut``
    payment coin that only deducts what it needs. ``payment_coin`` must
    therefore already hold exactly the amount the caller intends to fund;
    see :func:`~pytusk.core.ops.coins.prepare_wal_coin_for_amount`.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        shared_blob_object (str): Object ID of the ``SharedBlob`` to fund.
            Move's ``self: &mut SharedBlob`` argument.
        payment_coin (str | bcs.Argument): The ``Coin<WAL>`` to deposit in
            full -- either an existing coin's object ID, or the command
            result of a ``split_coin``/``merge_coins`` composed earlier in
            the same transaction. Move's ``added_funds: Coin<WAL>``
            argument (consumed).

    Returns:
        None.
    """
    await txn.move_call(
        target=f"{package_id}::shared_blob::fund",
        arguments=[shared_blob_object, payment_coin],
        type_arguments=[],
    )


async def add_extend_shared_blob(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    shared_blob_object: str,
    system_object: str,
    extended_epochs: int,
) -> None:
    """Add a single ``shared_blob::extend`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created.

    No payment coin is passed -- ``shared_blob::extend`` withdraws the
    ``SharedBlob``'s own pooled ``funds`` balance, calls
    ``system::extend_blob`` internally, and deposits back whatever remains
    (see the module docstring). This can abort on-chain if the pool's
    balance does not cover the extension cost; that balance is not
    pre-checkable client-side the way a caller's own WAL coin balance is.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        shared_blob_object (str): Object ID of the ``SharedBlob`` to
            extend. Move's ``self: &mut SharedBlob`` argument.
        system_object (str): Walrus ``System`` object ID. Move's
            ``system: &mut System`` argument.
        extended_epochs (int): Number of epochs to extend the wrapped
            ``Blob``'s storage by. Move's ``extended_epochs: u32``
            argument.

    Returns:
        None.
    """
    await txn.move_call(
        target=f"{package_id}::shared_blob::extend",
        arguments=[shared_blob_object, system_object, extended_epochs],
        type_arguments=[],
    )
