#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""COMPOSE layer for Blob metadata (Walrus "attribute") PTBs.

Contains :func:`add_set_blob_metadata`, :func:`add_drop_blob_metadata_keys`,
and :func:`add_drop_blob_metadata_all` -- each PURE PTB COMPOSITION: they
append one or more move_calls to a transaction the caller already created.
None makes a network call, builds, signs, or submits anything. Submission
lives in :mod:`pytusk.core.ops.blob_metadata_execute`, whose ``execute_*``
wrappers open a transaction, delegate all composition to the functions
here, then build, sign, and submit.

These operate on an ALREADY-REGISTERED ``Blob`` as a standalone,
post-registration transaction -- distinct from
:func:`~pytusk.core.ops.blob_compose.add_registration_sequence`'s
``attributes`` parameter, which composes the same
``insert_or_update_metadata_pair`` move_call INSIDE Tx1, against the
``Blob`` command result Tx1 itself just produced. Here the ``Blob`` already
exists on chain and is addressed by object ID.

NO-CLIENT INVARIANT: nothing in this module imports or references a
client / transaction-executor type (e.g.
:class:`~pytusk.client.walrus_client.WalrusClient`). A compose function
takes a ``txn: AsyncSuiTransaction`` and contributes to it; it never
submits.

Move entry points targeted (module ``walrus::blob``, every one of them
takes the ``Blob`` as ``&mut Blob`` -- no ``System`` object is involved)::

    insert_or_update_metadata_pair(self: &mut Blob, key: String, value: String)
    remove_metadata_pair(self: &mut Blob, key: &String): (String, String)
    take_metadata(self: &mut Blob): Metadata

``remove_metadata_pair``'s and ``take_metadata``'s return values are both
``drop``-able Move types (``(String, String)`` and ``Metadata`` respectively
-- ``Metadata`` is declared ``has drop, store`` in ``metadata.move``), so
neither result needs to be consumed by a further command or transferred;
Move drops them implicitly. Both Move functions abort with
``EMissingMetadata`` if the ``Blob`` carries no ``metadata`` dynamic field
at all, and ``remove_metadata_pair`` additionally aborts via
``vec_map::remove`` if the requested key is not present. Neither check is
pre-flighted here -- see
:mod:`pytusk.core.ops.blob_metadata_execute`'s pre-transaction existence
gate for the client-side check that avoids paying gas for a transaction
guaranteed to abort.
"""

from collections.abc import Mapping, Sequence

from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

__all__ = [
    "add_drop_blob_metadata_all",
    "add_drop_blob_metadata_keys",
    "add_set_blob_metadata",
]


async def add_set_blob_metadata(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    blob_object: str,
    pairs: Mapping[str, str],
) -> None:
    """Add one ``insert_or_update_metadata_pair`` move_call per pair to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends move_calls
    to the transaction the caller already created.

    Upsert semantics (confirmed against ``metadata.move``): each pair is
    inserted if its key is absent, or overwrites the existing value if the
    key is already present. There is no batch Move primitive, so one
    move_call is composed per pair, in ``pairs``' own iteration order --
    these are independent PTB commands taking pure ``String`` arguments,
    not a BCS map, so no canonical key ordering is imposed here (mirrors
    :func:`~pytusk.core.ops.blob_compose.add_registration_sequence`'s
    ``attributes`` handling).

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add move_calls to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to write metadata onto.
            Move's ``&mut Blob`` argument.
        pairs (Mapping[str, str]): Key/value pairs to insert or update. An
            empty mapping composes nothing.

    Returns:
        None.
    """
    for key, value in pairs.items():
        await txn.move_call(
            target=f"{package_id}::blob::insert_or_update_metadata_pair",
            arguments=[blob_object, key, value],
            type_arguments=[],
        )


async def add_drop_blob_metadata_keys(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    blob_object: str,
    keys: Sequence[str],
) -> None:
    """Add one ``remove_metadata_pair`` move_call per key to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends move_calls
    to the transaction the caller already created.

    There is no batch Move primitive -- Walrus's own Rust client loops one
    PTB call per key (``owned_blob_ops.rs``, ``remove_blob_attribute_pairs``)
    -- so this function does the same, one ``remove_metadata_pair`` call per
    key, in ``keys``' own order. Each call's ``(String, String)`` result is
    ``drop``-able and left unconsumed; see the module docstring.

    NO CLIENT-SIDE PRE-FLIGHT IS PERFORMED HERE -- a missing metadata field
    or a missing key surfaces as an on-chain ``EMissingMetadata``/
    ``vec_map::remove`` abort. Callers wanting the failure caught before any
    gas is spent should use
    :func:`~pytusk.core.ops.blob_metadata_execute.execute_drop_blob_metadata_keys`,
    which pre-flights existence via
    :func:`~pytusk.core.chain.blob_metadata.fetch_blob_metadata`.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add move_calls to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to remove metadata
            keys from. Move's ``&mut Blob`` argument.
        keys (Sequence[str]): Metadata keys to remove. An empty sequence
            composes nothing.

    Returns:
        None.
    """
    for key in keys:
        await txn.move_call(
            target=f"{package_id}::blob::remove_metadata_pair",
            arguments=[blob_object, key],
            type_arguments=[],
        )


async def add_drop_blob_metadata_all(
    *, txn: AsyncSuiTransaction, package_id: str, blob_object: str
) -> None:
    """Add a single ``take_metadata`` move_call to ``txn``.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created.

    ``take_metadata`` drops the ``Blob``'s entire ``metadata`` dynamic
    field in one call and returns the removed ``Metadata`` struct, which is
    ``drop``-able and left unconsumed here; see the module docstring.

    NO CLIENT-SIDE PRE-FLIGHT IS PERFORMED HERE -- a ``Blob`` with no
    metadata field at all surfaces as an on-chain ``EMissingMetadata``
    abort. Callers wanting the failure caught before any gas is spent
    should use
    :func:`~pytusk.core.ops.blob_metadata_execute.execute_drop_blob_metadata_all`,
    which pre-flights existence via
    :func:`~pytusk.core.chain.blob_metadata.fetch_blob_metadata`.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to drop all metadata
            from. Move's ``&mut Blob`` argument.

    Returns:
        None.
    """
    await txn.move_call(
        target=f"{package_id}::blob::take_metadata",
        arguments=[blob_object],
        type_arguments=[],
    )
