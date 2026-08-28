#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""COMPOSE layer for Walrus ``Storage`` object lifecycle PTBs, plus the
pure fuse-compatibility validators.

Contains :func:`add_split_by_epoch`, :func:`add_split_by_size`,
:func:`add_fuse`, and :func:`add_destroy_storage` -- each PURE PTB
COMPOSITION: they append a single move_call to a transaction the caller
already created and return whatever command result the caller may need.
None makes a network call, builds, signs, or submits anything. Submission
lives in :mod:`pytusk.core.ops.storage_execute`, whose ``execute_*``
wrappers open a transaction, delegate all composition to the functions
here, then build, sign, and submit.

Also contains :func:`fuse_incompatibility`, :func:`fuse_periods_incompatibility`,
and :func:`validate_fuse_pair` -- PURE, LOCAL validators (no network, no
transaction) that mirror ``storage_resource::fuse``'s dispatch and
assertion order, so a caller can be told client-side what Move itself
would have aborted with. They are grouped with the compose functions here
because they are the client-side pre-flight for :func:`add_fuse` /
:func:`~pytusk.core.ops.storage_execute.execute_fuse`, not because they
themselves compose a PTB.

NO-CLIENT INVARIANT: nothing in this module imports or references a
client / transaction-executor type (e.g.
:class:`~pytusk.client.walrus_client.WalrusClient`). A compose function
takes a ``txn: AsyncSuiTransaction`` and contributes to it; it never
submits.

Move entry points targeted (module ``walrus::storage_resource``; every one
of them operates on OWNED ``Storage`` objects -- no ``System`` object and no
``Coin<WAL>`` payment is involved)::

    split_by_epoch(storage: &mut Storage, split_epoch: u32, ctx): Storage
    split_by_size(storage: &mut Storage, split_size: u64, ctx): Storage
    fuse(first: &mut Storage, second: Storage)
    destroy(storage: Storage)

DELIBERATELY NOT HERE: ``system::extend_blob_with_resource``. It consumes a
``Storage`` but its SUBJECT is a ``Blob`` -- taking a ``Storage`` argument
does not make an operation a storage operation, any more than
``register_blob`` is one. It is composed inline in
``pytusk.tusky.tusky_cmds_storage`` alongside the other blob-lifecycle
commands. It does, however, reuse :func:`validate_fuse_pair` from this
module, since ``blob::extend_with_resource`` calls ``fuse_periods``
internally and is bound by the same compatibility rules (plus one of its
own -- the extension must end strictly later than the blob's current
storage).

NO EPOCH-EXPIRATION GATING EXISTS AT THIS LAYER. ``storage_resource`` has
no access to the current epoch -- no clock, no ``System`` reference, no
imports at all -- so every check it performs compares ``Storage`` fields
against each other or against a caller-supplied split point, never against
a live epoch. An "expired" ``Storage`` (one whose ``end_epoch`` has already
passed) can still be split, fused, and destroyed. Expiry only bites when a
``Storage`` is used to register a ``Blob``, where ``blob::new`` checks it
against a real epoch obtained via the ``System`` object. Any "is this still
useful?" filtering is therefore a CLIENT-side concern -- compare
:attr:`~pytusk.core.types.receipts.StorageObject.end_epoch` against a
separately fetched current epoch.
"""

from typing import cast

from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.core.types.receipts import StorageObject

__all__ = [
    "add_destroy_storage",
    "add_fuse",
    "add_split_by_epoch",
    "add_split_by_size",
    "fuse_incompatibility",
    "fuse_periods_incompatibility",
    "validate_fuse_pair",
]


def fuse_incompatibility(*, first: StorageObject, second: StorageObject) -> str | None:
    """Return why ``first`` and ``second`` cannot fuse, or ``None`` if they can.

    PURE -- no network calls, no transaction. This is the client-side
    pre-flight for :func:`add_fuse` /
    :func:`~pytusk.core.ops.storage_execute.execute_fuse`, and mirrors the
    dispatch and assertion ORDER of ``storage_resource::fuse`` exactly so a
    caller is told what Move itself would have aborted with, rather than a
    differently-derived complaint.

    ``fuse`` dispatches on ``start_epoch`` alone::

        first.start_epoch == second.start_epoch  -> fuse_amount
        otherwise                                -> fuse_periods

    ``fuse_amount`` requires the two to cover the IDENTICAL epoch range and
    sums their sizes. ``fuse_periods`` requires EQUAL sizes and ADJACENT
    ranges (in either direction) and joins the periods; it asserts size
    BEFORE adjacency, and this function preserves that order.

    Args:
        first (StorageObject): The storage that would be mutated in place
            (Move's ``&mut Storage`` argument).
        second (StorageObject): The storage that would be CONSUMED.

    Returns:
        str | None: A human-readable reason naming the Move abort this
        pre-empts, or ``None`` when the pair is compatible.
    """
    if first.object_id and first.object_id == second.object_id:
        return (
            f"Cannot fuse storage {first.object_id} with itself: "
            "two distinct Storage objects are required."
        )

    if first.start_epoch == second.start_epoch:
        # fuse_amount route: identical epoch range required.
        if first.end_epoch != second.end_epoch:
            return (
                "Storage objects share start_epoch "
                f"{first.start_epoch} but end at {first.end_epoch} and "
                f"{second.end_epoch}; fusing by amount requires an "
                "identical epoch range (Move abort: EIncompatibleEpochs)."
            )
        return None

    # fuse_periods route -- delegated so the two validators cannot drift.
    return fuse_periods_incompatibility(first=first, second=second)


def fuse_periods_incompatibility(
    *, first: StorageObject, second: StorageObject
) -> str | None:
    """Report why ``fuse_periods`` would abort on this pair, or ``None``.

    Mirrors ``storage_resource::fuse_periods`` directly, preserving its
    assertion order: size FIRST, then adjacency.

    Use THIS rather than :func:`fuse_incompatibility` whenever the Move
    path being validated calls ``fuse_periods`` unconditionally instead of
    going through ``fuse``'s ``start_epoch`` dispatch.
    ``blob::extend_with_resource`` is exactly that case: it calls
    ``blob.storage.fuse_periods(extension)`` outright, so a pair sharing a
    ``start_epoch`` is still judged by the period rules there, whereas
    :func:`fuse_incompatibility` would route it to the ``fuse_amount``
    rules and report the wrong reason.

    Args:
        first (StorageObject): The storage that absorbs the other.
        second (StorageObject): The storage being consumed.

    Returns:
        str | None: A human-readable reason the pair is incompatible, or
            ``None`` if ``fuse_periods`` would succeed.
    """
    if first.storage_size != second.storage_size:
        return (
            f"Storage sizes differ ({first.storage_size} vs "
            f"{second.storage_size}); fusing across epoch periods requires "
            "equal sizes (Move abort: EIncompatibleAmount)."
        )
    if first.end_epoch != second.start_epoch and first.start_epoch != second.end_epoch:
        return (
            f"Storage ranges [{first.start_epoch}, {first.end_epoch}) and "
            f"[{second.start_epoch}, {second.end_epoch}) are not adjacent; "
            "fusing by period requires one to begin exactly where the "
            "other ends (Move abort: EIncompatibleEpochs)."
        )
    return None


def validate_fuse_pair(*, first: StorageObject, second: StorageObject) -> None:
    """Raise :class:`ValueError` if ``first`` and ``second`` cannot fuse.

    PURE -- no network calls, no transaction. Thin raising wrapper over
    :func:`fuse_incompatibility` so the rule is implemented exactly once.
    Use this on the mutating path (fuse, extend-with-resource), where a
    silently discarded boolean would defeat the point of pre-flighting; use
    :func:`fuse_incompatibility` directly when merely FILTERING candidates,
    to avoid try/except as control flow.

    Args:
        first (StorageObject): The storage that would be mutated in place.
        second (StorageObject): The storage that would be CONSUMED.

    Returns:
        None.

    Raises:
        ValueError: If the pair cannot fuse; the message names the Move
            abort being pre-empted.
    """
    reason = fuse_incompatibility(first=first, second=second)
    if reason is not None:
        raise ValueError(reason)


async def add_split_by_epoch(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    storage_object_id: str,
    split_epoch: int,
) -> bcs.Argument:
    """Add a ``split_by_epoch`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created and returns the
    resulting command result.

    ``split_by_epoch`` narrows ``storage_object_id`` in place to cover
    ``[start_epoch, split_epoch)`` and returns a NEW ``Storage`` covering
    ``[split_epoch, end_epoch)``. Both carry the original's full
    ``storage_size``; capacity is not divided.

    WARNING: the returned ``Storage`` command result is an UNCONSUMED
    OBJECT RESULT. If the caller does not consume it -- typically via
    ``txn.transfer_objects(transfers=[storage], recipient=...)``, or by
    feeding it into a further move_call -- the PTB will ABORT when
    built/executed. This function deliberately does not transfer it itself,
    so the caller decides where the new ``Storage`` ends up. This mirrors
    :func:`~pytusk.core.ops.blob_compose.add_reserve_and_register`.

    Move asserts ``start_epoch < split_epoch < end_epoch``; a
    ``split_epoch`` outside that open interval aborts with
    ``EInvalidEpoch``.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        storage_object_id (str): Object ID of the ``Storage`` to split. It
            is Move's ``&mut Storage`` argument and is MUTATED in place,
            not consumed.
        split_epoch (int): Epoch at which to cut. Must lie strictly between
            the storage's ``start_epoch`` and ``end_epoch``.

    Returns:
        bcs.Argument: The ``split_by_epoch`` command result (the newly
        created ``Storage``). NOT transferred -- see the warning above.
    """
    return cast(
        bcs.Argument,
        await txn.move_call(
            target=f"{package_id}::storage_resource::split_by_epoch",
            arguments=[storage_object_id, split_epoch],
            type_arguments=[],
        ),
    )


async def add_split_by_size(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    storage_object_id: str,
    split_size: int,
) -> bcs.Argument:
    """Add a ``split_by_size`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created and returns the
    resulting command result.

    ``split_by_size`` narrows ``storage_object_id`` in place to
    ``split_size`` bytes and returns a NEW ``Storage`` holding the
    remainder (``original_size - split_size``). Both cover the original's
    full epoch range; the period is not divided.

    WARNING: the returned ``Storage`` command result is an UNCONSUMED
    OBJECT RESULT. If the caller does not consume it -- typically via
    ``txn.transfer_objects(transfers=[storage], recipient=...)``, or by
    feeding it into a further move_call -- the PTB will ABORT when
    built/executed. This function deliberately does not transfer it itself,
    so the caller decides where the new ``Storage`` ends up. This mirrors
    :func:`~pytusk.core.ops.blob_compose.add_reserve_and_register`.

    Move asserts ``storage_size >= split_size``; a larger ``split_size``
    aborts with ``EIncompatibleAmount``.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        storage_object_id (str): Object ID of the ``Storage`` to split. It
            is Move's ``&mut Storage`` argument and is MUTATED in place,
            not consumed.
        split_size (int): Byte count to retain on the original. Must not
            exceed the storage's current ``storage_size``.

    Returns:
        bcs.Argument: The ``split_by_size`` command result (the newly
        created ``Storage`` holding the remainder). NOT transferred -- see
        the warning above.
    """
    return cast(
        bcs.Argument,
        await txn.move_call(
            target=f"{package_id}::storage_resource::split_by_size",
            arguments=[storage_object_id, split_size],
            type_arguments=[],
        ),
    )


async def add_fuse(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    first_storage_id: str,
    second_storage_id: str,
) -> None:
    """Add a ``fuse`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created.

    ``fuse`` merges ``second_storage_id`` INTO ``first_storage_id`` and
    deletes the second. It has no return value, so unlike the ``split_*``
    helpers there is no command result to consume.

    Dispatch and the compatibility rules are documented on
    :func:`fuse_incompatibility`. This function does NOT pre-flight them --
    it is pure composition and makes no network calls, so it cannot fetch
    the two objects to compare. Call :func:`validate_fuse_pair` yourself,
    with already-fetched :class:`~pytusk.core.types.receipts.StorageObject`
    values, if you want an incompatible pair reported client-side rather
    than as an on-chain abort.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        first_storage_id (str): Object ID of the ``Storage`` to fuse INTO.
            Move's ``&mut Storage`` argument; MUTATED in place and
            survives.
        second_storage_id (str): Object ID of the ``Storage`` to fuse FROM.
            CONSUMED by value and deleted.

    Returns:
        None.
    """
    await txn.move_call(
        target=f"{package_id}::storage_resource::fuse",
        arguments=[first_storage_id, second_storage_id],
        type_arguments=[],
    )


async def add_destroy_storage(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    storage_object_id: str,
) -> None:
    """Add a ``destroy`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created.

    ``destroy`` CONSUMES the storage object and deletes its ``UID``. It has
    no return value and NO preconditions -- Move performs no checks at all,
    including no epoch check, so an unexpired reservation is destroyed just
    as readily as a spent one. The storage rebate is a Sui-level side
    effect of the deletion rather than anything the Move call itself does,
    which is why the tusky CLI surfaces this as ``reclaim_storage`` while
    this helper mirrors the Move function's own name.

    A ``Storage`` still WRAPPED inside a ``Blob`` cannot be passed here --
    its object ID does not resolve as a top-level object. Unwrap it first,
    e.g. via ``system::delete_blob``.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        storage_object_id (str): Object ID of the ``Storage`` to destroy.
            CONSUMED by value.

    Returns:
        None.
    """
    await txn.move_call(
        target=f"{package_id}::storage_resource::destroy",
        arguments=[storage_object_id],
        type_arguments=[],
    )
