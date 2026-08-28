#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""EXECUTE layer for Walrus ``Storage`` object lifecycle transactions.

Contains :func:`execute_split_by_epoch`, :func:`execute_split_by_size`,
:func:`execute_fuse`, and :func:`execute_destroy_storage`. Each is a THIN
CONVENIENCE WRAPPER around the matching ``add_*`` function in
:mod:`pytusk.core.ops.storage_compose` -- see that module's docstring for
the "caller owns the transaction lifecycle" split. They open a
transaction, delegate all composition to ``add_*``, then build, sign, and
submit.

``execute_fuse`` performs NO client-side pre-flight: it does not fetch the
two storage objects to compare them, so an incompatible pair surfaces as
an on-chain abort rather than a local error. Callers who want the failure
explained locally should fetch both objects and call
:func:`~pytusk.core.ops.storage_compose.validate_fuse_pair` first -- which
is what the tusky ``fuse_storage`` command does.
"""

from pysui import ExecuteTransaction
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.chain import find_created_object_id, require_success
from pytusk.core.ops.storage_compose import (
    add_destroy_storage,
    add_fuse,
    add_split_by_epoch,
    add_split_by_size,
)
from pytusk.core.types.receipts import SplitResult, StorageOpResult

__all__ = [
    "execute_destroy_storage",
    "execute_fuse",
    "execute_split_by_epoch",
    "execute_split_by_size",
]


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

    THIN WRAPPER around
    :func:`~pytusk.core.ops.storage_compose.add_split_by_epoch` -- see that
    module docstring's "caller owns the transaction lifecycle" note. All
    PTB composition lives there; this function only opens a transaction,
    delegates to it, consumes the returned ``Storage`` by transferring it
    to ``recipient``, then builds, signs, and submits.

    The new ``Storage`` MUST be consumed or the PTB would abort, so unlike
    the ``add_*`` layer this function makes the transfer decision for you.
    ``recipient`` defaults to the resolved sender, which keeps the split
    halves together in one wallet.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
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

    THIN WRAPPER around
    :func:`~pytusk.core.ops.storage_compose.add_split_by_size` -- see that
    module docstring's "caller owns the transaction lifecycle" note. All
    PTB composition lives there; this function only opens a transaction,
    delegates to it, consumes the returned ``Storage`` by transferring it
    to ``recipient``, then builds, signs, and submits.

    The new ``Storage`` MUST be consumed or the PTB would abort, so unlike
    the ``add_*`` layer this function makes the transfer decision for you.
    ``recipient`` defaults to the resolved sender, which keeps the split
    halves together in one wallet.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
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

    THIN WRAPPER around :func:`~pytusk.core.ops.storage_compose.add_fuse`
    -- see that module docstring's "caller owns the transaction lifecycle"
    note. All PTB composition lives there; this function only opens a
    transaction, delegates to it, then builds, signs, and submits. ``fuse``
    has no return value, so there is no command result to consume.

    NO CLIENT-SIDE PRE-FLIGHT IS PERFORMED HERE. This function does not
    fetch the two storage objects to compare them, so an incompatible pair
    surfaces as an on-chain abort rather than a local error. Callers who
    want the failure explained locally should fetch both objects and call
    :func:`~pytusk.core.ops.storage_compose.validate_fuse_pair` first --
    which is what the tusky ``fuse_storage`` command does.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
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

    THIN WRAPPER around
    :func:`~pytusk.core.ops.storage_compose.add_destroy_storage` -- see
    that module docstring's "caller owns the transaction lifecycle" note.
    All PTB composition lives there; this function only opens a
    transaction, delegates to it, then builds, signs, and submits.
    ``destroy`` has no return value, so there is no command result to
    consume.

    Move performs NO checks -- an unexpired reservation is destroyed just
    as readily as a spent one, and the capacity is not recoverable
    afterwards. Callers wanting an "is this still useful?" guard must apply
    it themselves by comparing
    :attr:`~pytusk.core.types.receipts.StorageObject.end_epoch` against a
    separately fetched current epoch; see
    :mod:`pytusk.core.ops.storage_compose`'s module docstring.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
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
