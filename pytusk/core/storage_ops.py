#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Storage-object lifecycle for Walrus: split, fuse, reclaim, and listing.

A ``Storage`` object is a raw capacity reservation -- a byte count held for
an epoch range -- and is the resource a ``Blob`` consumes when it is
registered. While a ``Storage`` is embedded in a ``Blob`` it is a WRAPPED
object: its ``UID`` is visible in the blob's contents but the object itself
cannot be fetched or addressed independently. It becomes an ordinary owned
object again only when unwrapped, e.g. by ``system::delete_blob`` (which
returns the storage intact) or by ``system::reserve_space`` /
``storage_resource::split_by_*`` (which create new ones).

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
commands.
It does, however, reuse :func:`validate_fuse_pair` from this module, since
``blob::extend_with_resource`` calls ``fuse_periods`` internally and is
bound by the same compatibility rules (plus one of its own -- the extension
must end strictly later than the blob's current storage).

NO EPOCH-EXPIRATION GATING EXISTS AT THIS LAYER. ``storage_resource`` has
no access to the current epoch -- no clock, no ``System`` reference, no
imports at all -- so every check it performs compares ``Storage`` fields
against each other or against a caller-supplied split point, never against
a live epoch. An "expired" ``Storage`` (one whose ``end_epoch`` has already
passed) can still be split, fused, and destroyed. Expiry only bites when a
``Storage`` is used to register a ``Blob``, where ``blob::new`` checks it
against a real epoch obtained via the ``System`` object. Any "is this still
useful?" filtering is therefore a CLIENT-side concern -- compare
:attr:`StorageObject.end_epoch` against a separately fetched current epoch.

CALLER OWNS THE TRANSACTION LIFECYCLE, matching
:mod:`pytusk.core.system_ops`. Two layers are kept deliberately separate:

- The ``add_*`` functions are PURE PTB composition: they append move_calls
  to a transaction the caller already created and return whatever command
  result the caller may need. They make no network calls, build nothing,
  sign nothing, and submit nothing.
- The ``execute_*`` functions are thin convenience wrappers: they open a
  transaction, delegate all composition to the matching ``add_*``, then
  build, sign, and submit.

:func:`fuse_incompatibility` and :func:`validate_fuse_pair` are pure and
local -- no network, no transaction -- and are the client-side pre-flight
for a fuse that would otherwise abort on-chain.
"""

from __future__ import annotations

import dataclasses
from typing import cast

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from dataclasses_json import DataClassJsonMixin
from pysui import ExecuteTransaction, GetObjectsForType, GetObjectsOwnedByAddress
from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction
from pysui.sui.sui_grpc.suimsgs.google import protobuf as pb

from pytusk.client.walrus_client import WalrusClient
from pytusk.config.tusk_config import NetworkType
from pytusk.core.utils import find_created_object_id, require_success

__all__ = [
    "SplitResult",
    "StorageObject",
    "StorageOpResult",
    "add_destroy_storage",
    "add_fuse",
    "add_split_by_epoch",
    "add_split_by_size",
    "execute_destroy_storage",
    "execute_fuse",
    "execute_split_by_epoch",
    "execute_split_by_size",
    "fuse_incompatibility",
    "fuse_periods_incompatibility",
    "list_storage_objects",
    "storage_from_blob",
    "storage_from_object",
    "validate_fuse_pair",
]


@dataclasses.dataclass(kw_only=True, frozen=True)
class StorageOpResult:
    """Outcome of a storage operation that creates no new object.

    Returned by :func:`execute_fuse` and :func:`execute_destroy_storage`.
    Both Move calls (``fuse``, ``destroy``) have no return value, so there
    is no created object to report -- only which storage was acted on and
    the transaction that did it.

    Attributes:
        object_id (str): Object ID of the ``Storage`` acted on. For a fuse
            this is the SURVIVING storage (the ``&mut`` argument); for a
            destroy it is the storage that was consumed, whose ID no longer
            resolves on-chain once the transaction lands.
        digest (str): The transaction digest.
    """

    object_id: str
    digest: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class SplitResult:
    """Outcome of splitting a ``Storage`` object.

    Returned by :func:`execute_split_by_epoch` and
    :func:`execute_split_by_size`. Unlike :class:`StorageOpResult`, a split
    DOES create a new object, and its ID is the value the caller most
    likely needs next.

    Attributes:
        object_id (str): Object ID of the ORIGINAL storage. Mutated in
            place by the split and still owned by the sender.
        new_object_id (str): Object ID of the ``Storage`` created by the
            split and transferred to the resolved recipient.
        digest (str): The transaction digest.
    """

    object_id: str
    new_object_id: str
    digest: str


@dataclasses.dataclass
class StorageObject(DataClassJsonMixin):
    """A standalone (unwrapped) Walrus ``Storage`` object.

    Mirrors the on-chain ``walrus::storage_resource::Storage`` struct. Also
    used to describe the ``Storage`` EMBEDDED in a ``Blob`` -- those fields
    are readable straight off the blob's parsed contents even though the
    wrapped object cannot be fetched by ID.

    Args:
        object_id (str): Sui object ID. Empty when describing a wrapped
            storage whose ID is not being tracked.
        start_epoch (int): First Walrus epoch the reservation covers.
        end_epoch (int): Epoch at which the reservation ends (EXCLUSIVE).
        storage_size (int): Reserved capacity in bytes.
    """

    object_id: str = dataclasses.field(default="")
    start_epoch: int = dataclasses.field(default=0)
    end_epoch: int = dataclasses.field(default=0)
    storage_size: int = dataclasses.field(default=0)


def fuse_incompatibility(*, first: StorageObject, second: StorageObject) -> str | None:
    """Return why ``first`` and ``second`` cannot fuse, or ``None`` if they can.

    PURE -- no network calls, no transaction. This is the client-side
    pre-flight for :func:`add_fuse` / :func:`execute_fuse`, and mirrors the
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
    :func:`~pytusk.core.system_ops.add_reserve_and_register`.

    Move asserts ``start_epoch < split_epoch < end_epoch``; a
    ``split_epoch`` outside that open interval aborts with
    ``EInvalidEpoch``.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.utils.resolve_package_id`.
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
    :func:`~pytusk.core.system_ops.add_reserve_and_register`.

    Move asserts ``storage_size >= split_size``; a larger ``split_size``
    aborts with ``EIncompatibleAmount``.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.utils.resolve_package_id`.
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
    with already-fetched :class:`StorageObject` values, if you want an
    incompatible pair reported client-side rather than as an on-chain
    abort.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.utils.resolve_package_id`.
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
            :func:`~pytusk.core.utils.resolve_package_id`.
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


async def execute_split_by_epoch(
    *,
    client: WalrusClient,
    package_id: str,
    storage_object_id: str,
    split_epoch: int,
    sender: str | None = None,
    sponsor: str | None = None,
    recipient: str | None = None,
) -> SplitResult:
    """Build and execute a ``split_by_epoch`` transaction.

    THIN WRAPPER around :func:`add_split_by_epoch` -- see the module
    docstring's "caller owns the transaction lifecycle" note. All PTB
    composition lives in :func:`add_split_by_epoch`; this function only
    opens a transaction, delegates to it, consumes the returned ``Storage``
    by transferring it to ``recipient``, then builds, signs, and submits.

    The new ``Storage`` MUST be consumed or the PTB would abort, so unlike
    the ``add_*`` layer this function makes the transfer decision for you.
    ``recipient`` defaults to the resolved sender, which keeps the split
    halves together in one wallet.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.utils.resolve_package_id`.
        storage_object_id (str): Object ID of the ``Storage`` to split.
        split_epoch (int): Epoch at which to cut. Must lie strictly between
            the storage's ``start_epoch`` and ``end_epoch``.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own ``storage_object_id``.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.
        recipient (str | None): Address to receive the newly created
            ``Storage``. Defaults to the resolved sender when ``None``.

    Returns:
        SplitResult: The original and newly created storage IDs, plus the
        transaction digest.

    Raises:
        RuntimeError: If transaction submission fails, the transaction
            aborts on-chain, or the created ``Storage`` cannot be located
            in the transaction effects.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    resolved_recipient = recipient or resolved_sender
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    storage = await add_split_by_epoch(
        txn=txn,
        package_id=package_id,
        storage_object_id=storage_object_id,
        split_epoch=split_epoch,
    )
    await txn.transfer_objects(transfers=[storage], recipient=resolved_recipient)
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"split_by_epoch transaction failed: {result.result_string}")
    effects = require_success(result_data=result.result_data, label="split_by_epoch")
    return SplitResult(
        object_id=storage_object_id,
        new_object_id=find_created_object_id(effects=effects, owner=resolved_recipient),
        digest=result.result_data.digest,
    )


async def execute_split_by_size(
    *,
    client: WalrusClient,
    package_id: str,
    storage_object_id: str,
    split_size: int,
    sender: str | None = None,
    sponsor: str | None = None,
    recipient: str | None = None,
) -> SplitResult:
    """Build and execute a ``split_by_size`` transaction.

    THIN WRAPPER around :func:`add_split_by_size` -- see the module
    docstring's "caller owns the transaction lifecycle" note. All PTB
    composition lives in :func:`add_split_by_size`; this function only
    opens a transaction, delegates to it, consumes the returned ``Storage``
    by transferring it to ``recipient``, then builds, signs, and submits.

    The new ``Storage`` MUST be consumed or the PTB would abort, so unlike
    the ``add_*`` layer this function makes the transfer decision for you.
    ``recipient`` defaults to the resolved sender, which keeps the split
    halves together in one wallet.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.utils.resolve_package_id`.
        storage_object_id (str): Object ID of the ``Storage`` to split.
        split_size (int): Byte count to retain on the original. Must not
            exceed the storage's current ``storage_size``.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own ``storage_object_id``.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.
        recipient (str | None): Address to receive the newly created
            ``Storage`` holding the remainder. Defaults to the resolved
            sender when ``None``.

    Returns:
        SplitResult: The original and newly created storage IDs, plus the
        transaction digest.

    Raises:
        RuntimeError: If transaction submission fails, the transaction
            aborts on-chain, or the created ``Storage`` cannot be located
            in the transaction effects.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    resolved_recipient = recipient or resolved_sender
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    storage = await add_split_by_size(
        txn=txn,
        package_id=package_id,
        storage_object_id=storage_object_id,
        split_size=split_size,
    )
    await txn.transfer_objects(transfers=[storage], recipient=resolved_recipient)
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"split_by_size transaction failed: {result.result_string}")
    effects = require_success(result_data=result.result_data, label="split_by_size")
    return SplitResult(
        object_id=storage_object_id,
        new_object_id=find_created_object_id(effects=effects, owner=resolved_recipient),
        digest=result.result_data.digest,
    )


async def execute_fuse(
    *,
    client: WalrusClient,
    package_id: str,
    first_storage_id: str,
    second_storage_id: str,
    sender: str | None = None,
    sponsor: str | None = None,
) -> StorageOpResult:
    """Build and execute a ``fuse`` transaction.

    THIN WRAPPER around :func:`add_fuse` -- see the module docstring's
    "caller owns the transaction lifecycle" note. All PTB composition lives
    in :func:`add_fuse`; this function only opens a transaction, delegates
    to it, then builds, signs, and submits. ``fuse`` has no return value,
    so there is no command result to consume.

    NO CLIENT-SIDE PRE-FLIGHT IS PERFORMED HERE. This function does not
    fetch the two storage objects to compare them, so an incompatible pair
    surfaces as an on-chain abort rather than a local error. Callers who
    want the failure explained locally should fetch both objects and call
    :func:`validate_fuse_pair` first -- which is what the tusky
    ``fuse_storage`` command does.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.utils.resolve_package_id`.
        first_storage_id (str): Object ID of the ``Storage`` to fuse INTO.
            Mutated in place and survives.
        second_storage_id (str): Object ID of the ``Storage`` to fuse FROM.
            Consumed and deleted.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own BOTH storage objects.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        StorageOpResult: The surviving storage ID and the transaction
        digest.

    Raises:
        RuntimeError: If transaction submission fails or the transaction
            aborts on-chain (including when the pair is incompatible --
            see the pre-flight note above).
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_fuse(
        txn=txn,
        package_id=package_id,
        first_storage_id=first_storage_id,
        second_storage_id=second_storage_id,
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"fuse transaction failed: {result.result_string}")
    require_success(result_data=result.result_data, label="fuse")
    return StorageOpResult(object_id=first_storage_id, digest=result.result_data.digest)


async def execute_destroy_storage(
    *,
    client: WalrusClient,
    package_id: str,
    storage_object_id: str,
    sender: str | None = None,
    sponsor: str | None = None,
) -> StorageOpResult:
    """Build and execute a ``destroy`` transaction.

    THIN WRAPPER around :func:`add_destroy_storage` -- see the module
    docstring's "caller owns the transaction lifecycle" note. All PTB
    composition lives in :func:`add_destroy_storage`; this function only
    opens a transaction, delegates to it, then builds, signs, and submits.
    ``destroy`` has no return value, so there is no command result to
    consume.

    Move performs NO checks -- an unexpired reservation is destroyed just
    as readily as a spent one, and the capacity is not recoverable
    afterwards. Callers wanting an "is this still useful?" guard must apply
    it themselves by comparing :attr:`StorageObject.end_epoch` against a
    separately fetched current epoch; see the module docstring.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.utils.resolve_package_id`.
        storage_object_id (str): Object ID of the ``Storage`` to destroy.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own ``storage_object_id``.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        StorageOpResult: The destroyed storage ID and the transaction
        digest.

    Raises:
        RuntimeError: If transaction submission fails or the transaction
            aborts on-chain.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_destroy_storage(
        txn=txn,
        package_id=package_id,
        storage_object_id=storage_object_id,
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"destroy transaction failed: {result.result_string}")
    require_success(result_data=result.result_data, label="destroy")
    return StorageOpResult(
        object_id=storage_object_id, digest=result.result_data.digest
    )


async def list_storage_objects(
    *, client: WalrusClient, owner: str, package_id: str
) -> list[StorageObject]:
    """List the standalone ``Storage`` objects owned by ``owner``.

    Neither an ``add_*`` nor an ``execute_*``: this composes no PTB and
    submits nothing. It is a read helper.

    On a PRODUCTION network (mainnet) the Walrus package address is
    stable, so this filters SERVER-SIDE via ``GetObjectsForType`` on
    ``package_id`` -- the node does the filtering, not this function.

    On any other network (e.g. testnet, whose Walrus contracts are
    periodically redeployed under a NEW package address) this instead
    lists EVERY owned object via ``GetObjectsOwnedByAddress`` and filters
    CLIENT-SIDE on the ``::storage_resource::Storage`` suffix of
    ``object_type``. Move type tags are fixed at mint time and never
    change when a package is upgraded, so a ``Storage`` minted under a
    PRIOR package version keeps that version's address in its type tag
    forever. Filtering server-side by the CURRENT ``package_id`` would
    silently miss it -- confirmed live against testnet: ``package_id``
    resolved from ``System.package_id`` reflects the CURRENT package, but
    existing ``Storage``/``Blob`` objects can carry an OLDER package
    address in their type tag, so a ``GetObjectsForType`` filter on the
    current address then matches nothing even though the objects exist.
    The suffix filter is package-address agnostic, so it finds a
    ``Storage`` regardless of which package version minted it.

    ``package_id`` is taken as a parameter rather than resolved here from
    the System object, so it is only actually used on the PRODUCTION path
    above -- ignored otherwise. A caller composing storage operations
    already holds it (every ``add_*``/``execute_*`` in this module
    requires it), and resolving it internally would add a hidden network
    round trip. Callers that do not have it can obtain one via
    :func:`~pytusk.core.utils.resolve_package_id`.

    Only UNWRAPPED storage is returned. A ``Storage`` still embedded in a
    ``Blob`` is a wrapped object with no independent owner record, so it
    does not appear in an owned-object listing at all -- see this module's
    docstring.

    No epoch filtering is applied. ``end_epoch`` may already have passed;
    such a ``Storage`` is still splittable, fusable, and destroyable. To
    show only still-useful reservations, compare
    :attr:`StorageObject.end_epoch` against a separately fetched current
    epoch (e.g. ``await client.walrus_epoch()``).

    Args:
        client (WalrusClient): Client used to query owned objects.
        owner (str): Address whose ``Storage`` objects are listed.
        package_id (str): Current Walrus package ID. Used to build the
            ``<package_id>::storage_resource::Storage`` type filter on a
            PRODUCTION network; ignored on any other network, where every
            owned object is scanned instead.

    Returns:
        list[StorageObject]: Every owned ``Storage``, in the order the node
            returned them. Empty when ``owner`` holds none.

    Raises:
        RuntimeError: If the objects cannot be listed.
    """
    if client.config.network.network_type == NetworkType.PRODUCTION:
        result = await client.execute_for_all(
            command=GetObjectsForType(
                owner=owner,
                object_type=f"{package_id}::storage_resource::Storage",
            )
        )
        if not result.is_ok():
            raise RuntimeError(
                f"Cannot list Storage objects for {owner}: {result.result_string}"
            )
        candidates = result.result_data.objects
    else:
        result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
        if not result.is_ok():
            raise RuntimeError(
                f"Cannot list Storage objects for {owner}: {result.result_string}"
            )
        candidates = [
            obj
            for obj in result.result_data.objects
            if obj.object_type
            and obj.object_type.endswith("::storage_resource::Storage")
        ]

    storages: list[StorageObject] = []
    for obj in candidates:
        try:
            storages.append(storage_from_object(obj=obj))
        except ValueError:
            # A listing entry with no JSON view is skipped rather than
            # failing the whole listing. A caller who fetched ONE object by
            # ID gets the ValueError instead, because there a missing view
            # means the specific thing they asked for is unreadable.
            continue
    return storages


def storage_from_object(*, obj: sui_prot.Object) -> StorageObject:
    """Parse a fetched Sui object into a :class:`StorageObject`.

    Shared by :func:`list_storage_objects` and by callers holding a single
    ``Storage`` fetched by ID -- notably the client-side pre-flight for a
    fuse, which needs both operands as :class:`StorageObject` before
    :func:`fuse_incompatibility` can judge them.

    ``start_epoch`` and ``end_epoch`` are ``u32`` and arrive as JSON
    numbers. ``storage_size`` is ``u64`` and arrives as a decimal STRING to
    avoid precision loss (the same reason a ``Blob``'s ``blob_id`` does).
    Either form is accepted for the size, so a proto-shape change degrades
    to a loud wrong parse rather than silently reporting every reservation
    as zero bytes.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus
            ``Storage``. The caller is responsible for checking
            ``object_type`` first -- this function reads fields and does
            not verify the object's type.

    Returns:
        StorageObject: The parsed reservation.

    Raises:
        ValueError: If the object carries no JSON struct view, so its
            fields cannot be read at all.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id or '(unknown)'} has no JSON view; "
            "its Storage fields cannot be read."
        )
    return _storage_from_field_map(
        fields=obj.json.struct_value.fields, object_id=obj.object_id or ""
    )


def storage_from_blob(*, obj: sui_prot.Object) -> StorageObject:
    """Read the ``Storage`` EMBEDDED in a fetched ``Blob`` object.

    A ``Blob`` holds its ``Storage`` by value, so the reservation's fields
    are readable straight off the blob's contents even though that wrapped
    object has no independent ID and cannot be fetched on its own. The
    returned :attr:`StorageObject.object_id` is therefore always ``""``.

    This is what a caller needs before extending a blob with a standalone
    ``Storage``: ``blob::extend_with_resource`` calls ``fuse_periods`` on
    the blob's existing storage, so the extension must satisfy
    :func:`fuse_periods_incompatibility` against THIS value.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus
            ``Blob``. The caller is responsible for checking
            ``object_type`` first -- this function reads fields and does
            not verify the object's type.

    Returns:
        StorageObject: The blob's embedded reservation, with an empty
            ``object_id``.

    Raises:
        ValueError: If the object carries no JSON view, or has no
            ``storage`` field.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id or '(unknown)'} has no JSON view; "
            "its embedded Storage fields cannot be read."
        )
    storage_val = obj.json.struct_value.fields.get("storage")
    if not (storage_val and storage_val.struct_value):
        raise ValueError(
            f"Object {obj.object_id or '(unknown)'} has no 'storage' "
            "field; it may not be a Walrus Blob."
        )
    return _storage_from_field_map(fields=storage_val.struct_value.fields, object_id="")


def _storage_from_field_map(
    *, fields: dict[str, pb.Value], object_id: str
) -> StorageObject:
    """Build a :class:`StorageObject` from a Storage struct's field map.

    Shared by :func:`storage_from_object` and :func:`storage_from_blob`,
    which differ only in how they reach these fields.

    ``start_epoch`` and ``end_epoch`` are ``u32`` and arrive as JSON
    numbers. ``storage_size`` is ``u64`` and arrives as a decimal STRING
    to avoid precision loss (the same reason a ``Blob``'s ``blob_id``
    does). Either form is accepted for the size, so a proto-shape change
    degrades to a loud wrong parse rather than silently reporting every
    reservation as zero bytes.

    Args:
        fields (dict[str, pb.Value]): The Storage struct's field map.
        object_id (str): Object ID to record, or ``""`` for a wrapped
            storage that has no independently addressable ID.

    Returns:
        StorageObject: The parsed reservation.
    """
    start_val = fields.get("start_epoch")
    end_val = fields.get("end_epoch")
    size_val = fields.get("storage_size")

    if size_val is None:
        storage_size = 0
    elif size_val.string_value:
        storage_size = int(size_val.string_value)
    else:
        storage_size = int(size_val.number_value or 0)

    return StorageObject(
        object_id=object_id,
        start_epoch=int(start_val.number_value or 0) if start_val else 0,
        end_epoch=int(end_val.number_value or 0) if end_val else 0,
        storage_size=storage_size,
    )
