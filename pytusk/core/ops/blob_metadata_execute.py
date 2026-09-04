#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""EXECUTE layer for Blob metadata (Walrus "attribute") transactions.

Contains :func:`execute_set_blob_metadata`,
:func:`execute_drop_blob_metadata_keys`, and
:func:`execute_drop_blob_metadata_all`. Each is a THIN CONVENIENCE WRAPPER
around the matching ``add_*`` function in
:mod:`pytusk.core.ops.blob_metadata_compose` -- see that module's docstring
for the "caller owns the transaction lifecycle" split. They open a
transaction, delegate all composition to ``add_*``, then build, sign, and
submit.

Pre-transaction existence gate (Frank, 2026-09-03): ``insert_or_update_metadata_pair``
always either upserts or aborts only on a bad object ID, so
:func:`execute_set_blob_metadata` needs no pre-check. ``remove_metadata_pair``
and ``take_metadata`` both abort with ``EMissingMetadata`` on a ``Blob``
with no metadata field at all, and ``remove_metadata_pair`` additionally
aborts if a requested key is absent -- both guaranteed-abort conditions
that a client-side read can catch before any gas is spent. Per the
project's error boundary (pre-spend raises, post-spend returns),
:func:`execute_drop_blob_metadata_keys` and
:func:`execute_drop_blob_metadata_all` therefore fetch the blob's current
metadata via :func:`~pytusk.core.chain.blob_metadata.fetch_blob_metadata`
FIRST and raise :class:`ValueError` -- the project's established
client-side-precondition exception, per
:func:`~pytusk.core.ops.storage_compose.validate_fuse_pair` -- before
composing any PTB, rather than letting the on-chain abort happen.
"""

from collections.abc import Mapping, Sequence

from pysui import ExecuteTransaction
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.chain import require_success
from pytusk.core.ops.blob_metadata_compose import (
    add_drop_blob_metadata_all,
    add_drop_blob_metadata_keys,
    add_set_blob_metadata,
)
from pytusk.core.types.receipts import BlobMetadataOpResult

__all__ = [
    "execute_drop_blob_metadata_all",
    "execute_drop_blob_metadata_keys",
    "execute_set_blob_metadata",
    "validate_blob_metadata_exists",
    "validate_blob_metadata_keys_exist",
]


async def validate_blob_metadata_keys_exist(
    *, client: WalrusClient, blob_object: str, keys: Sequence[str]
) -> None:
    """Raise :class:`ValueError` unless every key in ``keys`` currently exists.

    PRE-TRANSACTION EXISTENCE GATE, extracted so both
    :func:`execute_drop_blob_metadata_keys` (for SDK users calling it
    directly) and the ``tusky drop_blob_metadata --keys`` CLI handler
    (which now bypasses that wrapper to honor ``--mode``, per the project's
    "mode/simulate is a CLI-only concern" boundary) share exactly one
    implementation of this check rather than duplicating it. Fetches the
    blob's current metadata via
    :func:`~pytusk.client.walrus_client.WalrusClient.get_blob_metadata`. If
    the blob has no metadata field at all, or any requested key is not
    present in it, raises :class:`ValueError` naming what is missing --
    pre-spend, per the project's error boundary, rather than letting
    ``remove_metadata_pair``'s guaranteed ``EMissingMetadata``/
    ``vec_map::remove`` abort happen after gas has already been spent.

    Args:
        client (WalrusClient): Client used to fetch existing metadata.
        blob_object (str): Object ID of the ``Blob`` being checked.
        keys (Sequence[str]): Metadata keys that must already exist.

    Returns:
        None.

    Raises:
        ValueError: If the blob has no metadata at all, or if any requested
            key is not currently present.
    """
    existing = await client.get_blob_metadata(blob_object=blob_object)
    if existing is None:
        raise ValueError(
            f"Blob {blob_object} has no metadata set at all; cannot remove "
            f"keys {list(keys)!r} (Move abort: EMissingMetadata)."
        )
    existing_keys = {entry.key for entry in existing.data}
    missing_keys = [key for key in keys if key not in existing_keys]
    if missing_keys:
        raise ValueError(
            f"Blob {blob_object} has no metadata entry for key(s) "
            f"{missing_keys!r}; current keys are {sorted(existing_keys)!r} "
            "(Move abort: vec_map::remove would fail)."
        )


async def validate_blob_metadata_exists(
    *, client: WalrusClient, blob_object: str
) -> None:
    """Raise :class:`ValueError` unless ``blob_object`` has any metadata at all.

    PRE-TRANSACTION EXISTENCE GATE, extracted so both
    :func:`execute_drop_blob_metadata_all` (for SDK users calling it
    directly) and the ``tusky drop_blob_metadata --all`` CLI handler
    (which now bypasses that wrapper to honor ``--mode``, per the project's
    "mode/simulate is a CLI-only concern" boundary) share exactly one
    implementation of this check rather than duplicating it. Fetches the
    blob's current metadata via
    :func:`~pytusk.client.walrus_client.WalrusClient.get_blob_metadata`. If
    the blob has no metadata field at all (``None``) or an empty pair set,
    raises :class:`ValueError` -- pre-spend, per the project's error
    boundary, rather than letting ``take_metadata``'s guaranteed
    ``EMissingMetadata`` abort happen after gas has already been spent.

    Args:
        client (WalrusClient): Client used to fetch existing metadata.
        blob_object (str): Object ID of the ``Blob`` being checked.

    Returns:
        None.

    Raises:
        ValueError: If the blob has no metadata at all.
    """
    existing = await client.get_blob_metadata(blob_object=blob_object)
    if existing is None or not existing.data:
        raise ValueError(
            f"Blob {blob_object} has no metadata set at all; nothing to "
            "drop (Move abort: EMissingMetadata)."
        )


async def execute_set_blob_metadata(
    *,
    client: WalrusClient,
    package_id: str,
    blob_object: str,
    pairs: Mapping[str, str],
    sender: str | None = None,
    sponsor: str | None = None,
) -> BlobMetadataOpResult:
    """Build and execute a batch of ``insert_or_update_metadata_pair`` calls.

    THIN WRAPPER around
    :func:`~pytusk.core.ops.blob_metadata_compose.add_set_blob_metadata` --
    see that module docstring's "caller owns the transaction lifecycle"
    note. All PTB composition lives there; this function only opens a
    transaction, delegates to it, then builds, signs, and submits.

    NO PRE-TRANSACTION GATE: upsert semantics mean this always either
    inserts or overwrites, aborting only on a malformed ``blob_object`` --
    the same class of failure any PTB targeting a bad object ID would hit,
    not a condition a client-side read could usefully pre-empt.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to write metadata onto.
        pairs (Mapping[str, str]): Key/value pairs to insert or update.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own ``blob_object``.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        BlobMetadataOpResult: The acted-on blob's object ID and the
        transaction digest.

    Raises:
        RuntimeError: If transaction submission fails or the transaction
            aborts on-chain.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_set_blob_metadata(
        txn=txn, package_id=package_id, blob_object=blob_object, pairs=pairs
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"set_blob_metadata transaction failed: {result.result_string}")
    require_success(result_data=result.result_data, label="set_blob_metadata")
    return BlobMetadataOpResult(object_id=blob_object, digest=result.result_data.digest)


async def execute_drop_blob_metadata_keys(
    *,
    client: WalrusClient,
    package_id: str,
    blob_object: str,
    keys: Sequence[str],
    sender: str | None = None,
    sponsor: str | None = None,
) -> BlobMetadataOpResult:
    """Build and execute a batch of ``remove_metadata_pair`` calls.

    THIN WRAPPER around
    :func:`~pytusk.core.ops.blob_metadata_compose.add_drop_blob_metadata_keys`
    -- see that module docstring's "caller owns the transaction lifecycle"
    note. All PTB composition lives there; this function opens a
    transaction, delegates to it, then builds, signs, and submits.

    PRE-TRANSACTION EXISTENCE GATE, before any PTB is composed: fetches the
    blob's current metadata via
    :func:`~pytusk.core.chain.blob_metadata.fetch_blob_metadata`. If the
    blob has no metadata field at all, or any requested key is not present
    in it, raises :class:`ValueError` naming what is missing -- pre-spend,
    per the project's error boundary, rather than letting
    ``remove_metadata_pair``'s guaranteed ``EMissingMetadata``/
    ``vec_map::remove`` abort happen after gas has already been spent.

    Args:
        client (WalrusClient): Client used to fetch existing metadata and
            submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to remove metadata
            keys from.
        keys (Sequence[str]): Metadata keys to remove. Every key must
            already exist on the blob.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own ``blob_object``.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        BlobMetadataOpResult: The acted-on blob's object ID and the
        transaction digest.

    Raises:
        ValueError: If the blob has no metadata at all, or if any requested
            key is not currently present -- pre-spend, before any PTB is
            composed.
        RuntimeError: If transaction submission fails or the transaction
            aborts on-chain.
    """
    await validate_blob_metadata_keys_exist(
        client=client, blob_object=blob_object, keys=keys
    )

    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_drop_blob_metadata_keys(
        txn=txn, package_id=package_id, blob_object=blob_object, keys=keys
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(
            f"drop_blob_metadata_keys transaction failed: {result.result_string}"
        )
    require_success(result_data=result.result_data, label="drop_blob_metadata_keys")
    return BlobMetadataOpResult(object_id=blob_object, digest=result.result_data.digest)


async def execute_drop_blob_metadata_all(
    *,
    client: WalrusClient,
    package_id: str,
    blob_object: str,
    sender: str | None = None,
    sponsor: str | None = None,
) -> BlobMetadataOpResult:
    """Build and execute a single ``take_metadata`` call.

    THIN WRAPPER around
    :func:`~pytusk.core.ops.blob_metadata_compose.add_drop_blob_metadata_all`
    -- see that module docstring's "caller owns the transaction lifecycle"
    note. All PTB composition lives there; this function opens a
    transaction, delegates to it, then builds, signs, and submits.

    PRE-TRANSACTION EXISTENCE GATE, before any PTB is composed: fetches the
    blob's current metadata via
    :func:`~pytusk.core.chain.blob_metadata.fetch_blob_metadata`. If the
    blob has no metadata field at all (``None``) or an empty pair set,
    raises :class:`ValueError` -- pre-spend, per the project's error
    boundary, rather than letting ``take_metadata``'s guaranteed
    ``EMissingMetadata`` abort happen after gas has already been spent.

    Args:
        client (WalrusClient): Client used to fetch existing metadata and
            submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to drop all metadata
            from.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own ``blob_object``.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        BlobMetadataOpResult: The acted-on blob's object ID and the
        transaction digest.

    Raises:
        ValueError: If the blob has no metadata at all -- pre-spend, before
            any PTB is composed.
        RuntimeError: If transaction submission fails or the transaction
            aborts on-chain.
    """
    await validate_blob_metadata_exists(client=client, blob_object=blob_object)

    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_drop_blob_metadata_all(
        txn=txn, package_id=package_id, blob_object=blob_object
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(
            f"drop_blob_metadata_all transaction failed: {result.result_string}"
        )
    require_success(result_data=result.result_data, label="drop_blob_metadata_all")
    return BlobMetadataOpResult(object_id=blob_object, digest=result.result_data.digest)
