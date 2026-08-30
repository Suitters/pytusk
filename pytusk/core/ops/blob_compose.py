#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""COMPOSE layer for blob registration and certification PTBs.

Contains :func:`add_reserve_and_register` (``reserve_space`` +
``register_blob``, Tx1) and :func:`add_certify` (``certify_blob``, Tx2).
Both are PURE PTB COMPOSITION: they append move_calls to a transaction the
caller already created and return whatever command result the caller may
need. Neither makes a network call, builds, signs, or submits anything.
This is what lets an SDK developer hold a transaction, choose its
sender/sponsor, interleave their own move_calls, and decide
simulate-vs-execute for themselves.

NO-CLIENT INVARIANT: nothing in this module imports or references a
client / transaction-executor type (e.g.
:class:`~pytusk.client.walrus_client.WalrusClient`). A compose function
takes a ``txn: AsyncSuiTransaction`` and contributes to it; it never
submits. Submission lives in :mod:`pytusk.core.ops.blob_execute`, whose
``execute_*`` wrappers open a transaction, delegate all composition to the
functions here, then build, sign, and submit.

Move entry points targeted (module ``walrus::system``, ``System`` is a
SHARED object)::

    reserve_space(self: &mut System, storage_amount: u64, epochs_ahead: u32,
                   payment: &mut Coin<WAL>, ctx): Storage
    register_blob(self: &mut System, storage: Storage, blob_id: u256,
                   root_hash: u256, size: u64, encoding_type: u8,
                   deletable: bool, write_payment: &mut Coin<WAL>, ctx): Blob
    certify_blob(self: &mut System, blob: &mut Blob, signature: vector<u8>,
                 signers_bitmap: vector<u8>, message: vector<u8>)

``Coin<WAL>`` arguments are ``&mut`` and split internally by the Move
functions -- they are passed by object ID and are NOT consumed/returned by
the PTB.

:func:`add_reserve_and_register` needs ``_encoded_storage_amount`` to
compute ``reserve_space``'s ``storage_amount`` argument. That helper lives
here rather than in :mod:`pytusk.core.ops.blob_execute` since it is a pure
compute-at-compose-time helper -- it is imported from here by
``blob_execute`` (a direction that is not circular, since ``blob_execute``
already imports :func:`add_reserve_and_register` and :func:`add_certify`
from this module for its ``execute_*`` wrappers).
"""

from collections.abc import Mapping
from typing import cast

from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.core.certification import Certificate
from pytusk.core.encoding import RS2_ENCODING_TYPE, EncodedBlob, encoded_blob_length
from pytusk.core.ops.tip_compose import add_tip
from pytusk.core.types import TipComposition

__all__ = [
    "add_certify",
    "add_registration_sequence",
    "add_reserve_and_register",
]


def _encoded_storage_amount(*, encoded: EncodedBlob) -> int:
    """Return the ENCODED storage size, in bytes, that ``reserve_space``
    requires as its ``storage_amount`` argument.

    Computed via :func:`~pytusk.core.encoding.encoded_blob_length`, which
    ports ``encoded_blob_length`` in ``redstuff.move``. The MOVE version is
    authoritative here, not the Rust ``walrus_core`` crate:
    ``reserve_space``'s ``storage_amount`` is charged on-chain against the
    Move contract's own computation, so an implementation that matched the
    Rust crate but not ``redstuff.move`` would still abort the transaction.

    Args:
        encoded (EncodedBlob): The already RedStuff-encoded blob whose
            on-chain encoded storage footprint is required.

    Returns:
        int: The ``storage_amount``, in bytes, to pass to ``reserve_space``.
    """
    return encoded_blob_length(
        unencoded_length=encoded.unencoded_length, n_shards=encoded.n_shards
    )


async def add_reserve_and_register(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    system_object: str,
    encoded: EncodedBlob,
    epochs: int,
    deletable: bool,
    payment_coin: str,
) -> bcs.Argument:
    """Add ``reserve_space`` + ``register_blob`` move_calls to ``txn`` (Tx1).

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends two
    move_calls to the transaction the caller already created and returns
    the resulting command result. ``reserve_space``'s ``Storage`` result is
    passed DIRECTLY as the ``storage`` argument to ``register_blob`` -- a
    command result flowing into a later command's ``arguments`` list, the
    same mechanism ``delete_blob`` (``pytusk.tusky.tusky_cmds_lifecycle``)
    and ``exchange_for_wal`` (``pytusk.tusky.tusky_cmds_exchange``) already
    use for their own command results.

    WARNING: the returned ``Blob`` command result is an UNCONSUMED OBJECT
    RESULT. If the caller does not consume it -- typically via
    ``txn.transfer_objects(transfers=[blob], recipient=...)``, or by feeding
    it into a further move_call -- the PTB will ABORT when built/executed.
    This function deliberately does not transfer it itself, so the caller
    decides where the newly registered ``Blob`` ends up.

    ``payment_coin`` is supplied BY THE CALLER -- an SDK developer knows
    their own fully-qualified WAL coin type and selects a coin from their
    own wallet (e.g. via pysui's ``GetCoins``); this function does not
    query or select one. ``Coin<WAL>`` is passed by object ID and is
    ``&mut`` in Move -- ``reserve_space``/``register_blob`` split what they
    need internally and leave the remainder in place; it is not
    consumed/returned by the PTB.

    ``storage_amount`` is computed via ``_encoded_storage_amount`` (this
    module), which ports ``encoded_blob_length`` from ``redstuff.move`` --
    the MOVE version is authoritative since ``reserve_space``'s
    ``storage_amount`` is charged on-chain against Move's own computation.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add move_calls to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        system_object (str): Object ID of the configured Walrus System
            object.
        encoded (EncodedBlob): The RedStuff-encoded blob to register.
        epochs (int): Number of epochs ahead to reserve storage for
            (``epochs_ahead`` on ``reserve_space``).
        deletable (bool): Whether the registered blob should be deletable.
        payment_coin (str): Object ID of a ``Coin<WAL>`` owned by the
            transaction's sender, used as ``&mut Coin<WAL>`` payment for
            both move_calls.

    Returns:
        bcs.Argument: The ``register_blob`` command result (the newly
        registered ``Blob``). NOT transferred -- see the warning above; the
        caller must consume it before building the transaction.
    """
    storage_amount = _encoded_storage_amount(encoded=encoded)
    storage = cast(
        bcs.Argument,
        await txn.move_call(
            target=f"{package_id}::system::reserve_space",
            arguments=[system_object, storage_amount, epochs, payment_coin],
            type_arguments=[],
        ),
    )
    blob = cast(
        bcs.Argument,
        await txn.move_call(
            target=f"{package_id}::system::register_blob",
            arguments=[
                system_object,
                storage,
                encoded.blob_id_u256,
                encoded.root_hash_u256,
                encoded.unencoded_length,
                RS2_ENCODING_TYPE,
                deletable,
                payment_coin,
            ],
            type_arguments=[],
        ),
    )
    return blob


async def add_registration_sequence(
    *,
    txn: AsyncSuiTransaction,
    encoded: EncodedBlob,
    epochs: int,
    deletable: bool,
    package_id: str,
    system_object: str,
    recipient: str,
    wal_payment_coin: str,
    tip: TipComposition | None = None,
    attributes: Mapping[str, str] | None = None,
) -> None:
    """Compose the whole of Tx1 -- optional tip, then reserve+register, then
    any attribute writes, then the transfer that consumes the new ``Blob``
    -- onto ``txn``, in that fixed order.

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends commands to
    the transaction the caller already created. It is the single seam both
    :func:`~pytusk.core.pipelines.write.store_blob_relay` (which
    bundles a tip into Tx1) and
    :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register`
    (which never has a tip) now compose Tx1 through, so the two can no
    longer drift apart the way the CLI's ad hoc ``simulate`` assembly once
    did from the pipeline's.

    Order is load-bearing and mirrors exactly what
    :func:`~pytusk.core.pipelines.write.store_blob_relay` composed by
    hand before this function existed:

    1. When ``tip`` is given, :func:`~pytusk.core.ops.tip_compose.add_tip`
       runs FIRST, before anything else touches ``txn`` -- the relay reads
       the authentication package at PTB input 0, and ``add_tip`` itself
       refuses to compose into a transaction that already holds an input or
       a command (see its docstring).
    2. :func:`add_reserve_and_register` runs next, registering the blob.
    3. When ``attributes`` is given, one
       ``walrus::blob::insert_or_update_metadata_pair`` call per pair runs
       against the ``Blob`` command result -- necessarily after step 2,
       which produces it, and before step 4, which consumes it.
    4. The returned ``Blob`` command result is transferred to ``recipient``
       via ``txn.transfer_objects`` -- an unconsumed object result would
       otherwise abort the PTB when built (see
       :func:`add_reserve_and_register`'s own warning).

    ``wal_payment_coin`` is REQUIRED here (unlike
    :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register`'s
    optional ``payment_coin``, which falls back to
    :func:`~pytusk.core.ops.coins.select_wal_payment_coin`): resolving "no coin
    given" into a concrete coin id needs a client and a network round trip,
    which this COMPOSE function -- client-free by the same invariant
    :func:`add_reserve_and_register` observes -- cannot perform. Callers
    resolve it first, e.g. via
    :func:`~pytusk.core.ops.blob_execute.preflight_payment`.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add commands to.
        encoded (EncodedBlob): The RedStuff-encoded blob to register.
        epochs (int): Number of epochs ahead to reserve storage for
            (``epochs_ahead`` on ``reserve_space``).
        deletable (bool): Whether the registered blob should be deletable.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        system_object (str): Object ID of the configured Walrus System
            object.
        recipient (str): Address the newly registered ``Blob`` is
            transferred to -- the resolved sender in every caller today
            (see :func:`add_reserve_and_register`'s docstring for why a
            third-party recipient belongs in Tx2, not here).
        wal_payment_coin (str): Object ID of a ``Coin<WAL>`` owned by the
            transaction's sender, already resolved by the caller.
        tip (TipComposition | None): When given, everything
            :func:`~pytusk.core.ops.tip_compose.add_tip` needs to bundle a
            relay tip payment into this same PTB, composed first. ``None``
            (the default) omits the tip entirely.
        attributes (Mapping[str, str] | None): Key/value pairs written onto
            the new ``Blob`` as on-chain metadata, one PTB command each.
            ``None`` (the default) writes none. Pairs are composed in the
            mapping's own iteration order: these are INDEPENDENT PTB
            commands taking pure ``String`` arguments, NOT a BCS map, so the
            canonical key ordering that governs a serialized map (see
            :func:`~pytusk.core.encoding.quilt.sorted_tag_entries`) does not
            apply and no sort is imposed here.

    Returns:
        None.

    Raises:
        TipPaymentError: If ``tip`` is given and ``txn`` already holds any
            input or command -- propagated verbatim from
            :func:`~pytusk.core.ops.tip_compose.add_tip`.
    """
    if tip is not None:
        await add_tip(
            txn=txn,
            relay_address=tip.relay_address,
            tip_amount=tip.tip_amount,
            auth_package=tip.auth_package,
            payment_coin=tip.payment_coin,
        )

    blob = await add_reserve_and_register(
        txn=txn,
        package_id=package_id,
        system_object=system_object,
        encoded=encoded,
        epochs=epochs,
        deletable=deletable,
        payment_coin=wal_payment_coin,
    )

    if attributes:
        # insert_or_update_metadata_pair routes through metadata_or_create,
        # which attaches the metadata dynamic field on first use -- a freshly
        # registered Blob needs no add_metadata call ahead of this.
        for key, value in attributes.items():
            await txn.move_call(
                target=f"{package_id}::blob::insert_or_update_metadata_pair",
                arguments=[blob, key, value],
                type_arguments=[],
            )

    await txn.transfer_objects(transfers=[blob], recipient=recipient)


async def add_certify(
    *,
    txn: AsyncSuiTransaction,
    package_id: str,
    system_object: str,
    blob_object_id: str,
    certificate: Certificate,
    recipient: str | None = None,
) -> None:
    """Add a ``certify_blob`` move_call to ``txn`` (Tx2).

    PURE PTB COMPOSITION -- see the module docstring's "caller owns the
    transaction lifecycle" note. This makes no network calls, builds
    nothing, signs nothing, and submits nothing; it only appends one
    move_call to the transaction the caller already created.
    ``certify_blob`` has no return value (it mutates the referenced
    ``Blob`` in place), so unlike :func:`add_reserve_and_register` there is
    no command result to return or consume.

    ``certificate.aggregate_signature``, ``certificate.signers_bitmap``, and
    ``certificate.serialized_message`` are passed VERBATIM as ``vector<u8>``
    pure arguments -- the confirmation message is NEVER reconstructed
    client-side. This mirrors :class:`Certificate`'s own docstring, which
    states these fields map directly onto
    ``certify_blob(blob, signature, signers_bitmap, message)``.

    EPOCH-BOUND WARNING: ``certificate`` is only valid against the
    committee ordering/epoch it was built from -- if the on-chain epoch has
    moved on, ``certify_blob`` will abort. A caller composing Tx2 by hand
    should check
    :func:`~pytusk.core.native_upload.assert_certificate_epoch_current`
    before calling this, or handle the on-chain abort themselves.
    ``certify()`` in ``pytusk.core.native_upload`` already does the
    refetch-and-rebuild dance around this for callers who want it handled
    automatically.

    When ``recipient`` is given, a ``transfer_objects`` command is appended
    to ``txn`` AFTER the ``certify_blob`` move_call, transferring
    ``blob_object_id`` to ``recipient`` in the SAME PTB. This is what fixes
    the sender/owner mismatch at the heart of the recipient defect:
    ``certify_blob`` takes the blob as ``&mut Blob``, so the caller (i.e.
    the address that must own the object at simulation/execution time) must
    still be the object's owner when Tx2 runs. Composing the transfer here,
    after certification, in the same transaction, means the object is only
    ever handed to a different owner once it is safely certified -- and
    atomically so: if ``certify_blob`` aborts, the transfer never happens.
    :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register` (Tx1)
    is the reason this is necessary: it always transfers the newly
    registered ``Blob`` to the resolved sender, never to an arbitrary
    recipient, so that the sender still owns (and can sign for) the object
    when Tx2 certifies it.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        system_object (str): Object ID of the configured Walrus System
            object.
        blob_object_id (str): Object ID of the ``Blob`` to certify (e.g.
            ``Registration.object_id``).
        certificate (Certificate): The quorum-backed certificate to submit.
            See the epoch-bound warning above.
        recipient (str | None): Sui address to transfer the now-certified
            ``Blob`` to, in the same PTB, immediately after
            ``certify_blob``. Treated as a valid Sui address and used
            verbatim -- NOT validated. When ``None`` (the default), no
            transfer is added and the ``Blob`` stays with whoever already
            owns it (the resolved sender from Tx1).

    Returns:
        None.
    """
    await txn.move_call(
        target=f"{package_id}::system::certify_blob",
        arguments=[
            system_object,
            blob_object_id,
            certificate.aggregate_signature,
            certificate.signers_bitmap,
            certificate.serialized_message,
        ],
        type_arguments=[],
    )
    if recipient is not None:
        await txn.transfer_objects(transfers=[blob_object_id], recipient=recipient)
