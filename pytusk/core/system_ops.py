#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""On-chain PTB layer for native Walrus upload: ``reserve_space`` +
``register_blob`` (Tx1, composed in one PTB) and ``certify_blob`` (Tx2).

Everything Sui-level here is built through pysui, following the exact PTB
patterns already proven in ``pytusk.tusky.tusky_cmds`` (``extend_blob_expiration``,
``delete_blob``, ``burn_blob``, ``exchange_for_wal``): a transaction is opened
via :meth:`~pytusk.client.walrus_client.WalrusClient.transaction`, Move calls
are issued with ``txn.move_call(target=..., arguments=[...], type_arguments=[])``,
a command's result is threaded directly into a later command's ``arguments``
list (never fetched back as a standalone object), and the built PTB is signed
and submitted with ``ExecuteTransaction(**txdict)``. Status is read from
``result_data.effects.status`` -- NOT ``result_data.transaction.effects.status``
-- matching the confirmed pysui result shape used throughout
``tests/integration_tests/conftest.py``.

Move entry points targeted (module ``walrus::system``, ``System`` is a SHARED
object)::

    reserve_space(self: &mut System, storage_amount: u64, epochs_ahead: u32,
                   payment: &mut Coin<WAL>, ctx): Storage
    register_blob(self: &mut System, storage: Storage, blob_id: u256,
                   root_hash: u256, size: u64, encoding_type: u8,
                   deletable: bool, write_payment: &mut Coin<WAL>, ctx): Blob
    certify_blob(self: &mut System, blob: &mut Blob, signature: vector<u8>,
                 signers_bitmap: vector<u8>, message: vector<u8>)

``Coin<WAL>`` arguments are ``&mut`` and split internally by the Move
functions -- they are passed by object ID and are NOT consumed/returned by
the PTB, matching how ``extend_blob_expiration`` passes its WAL
``payment_coin_id`` straight through.

``_encoded_storage_amount`` computes ``reserve_space``'s ``storage_amount``
via :func:`~pytusk.core.encoding.encoded_blob_length`, which ports
``encoded_blob_length`` in ``redstuff.move`` -- the MOVE version, not the
Rust ``walrus_core`` crate, since ``reserve_space`` is charged on-chain
against Move's own computation.

CALLER OWNS THE TRANSACTION LIFECYCLE. Two layers are deliberately kept
separate here:

- :func:`add_reserve_and_register` and :func:`add_certify` are PURE PTB
  composition: they add move_calls to a transaction the caller already
  created and return whatever command result the caller may need. They make
  no network calls, build nothing, sign nothing, and submit nothing. This is
  what lets an SDK developer hold a transaction, choose its sender/sponsor,
  interleave their own move_calls, and decide simulate-vs-execute for
  themselves.
- :func:`execute_reserve_and_register` and :func:`execute_certify` are thin
  convenience wrappers around the two functions above: they open a
  transaction, delegate all PTB composition to ``add_*``, then build, sign,
  and submit it. They exist for a caller who wants the old one-call
  behaviour and does not need to compose Tx1/Tx2 by hand.
"""

import asyncio
import dataclasses
from typing import cast

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import (
    ExecuteTransaction,
    GetAddressCoinBalances,
    GetCoins,
    GetObject,
    GetTransaction,
)
from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.certification import Certificate
from pytusk.core.encoding import RS2_ENCODING_TYPE, EncodedBlob, encoded_blob_length

__all__ = [
    "DEFAULT_FINALITY_MAX_ATTEMPTS",
    "DEFAULT_FINALITY_MAX_DELAY",
    "CertifyResult",
    "Registration",
    "add_certify",
    "add_reserve_and_register",
    "execute_certify",
    "execute_reserve_and_register",
    "find_created_object_id",
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

_RESUME_HINT: str = (
    "The sliver fan-out had NOT yet run at this point, so no storage node "
    "holds this blob's slivers and none will sign a confirmation for it -- "
    "`tusky certify_blob` CANNOT recover this registration, because it only "
    "collects confirmations and submits Tx2, and has no source bytes to "
    "upload. Re-run the upload from the start; the paid storage on this "
    "registration is stranded."
)

_RESUME_HINT_PENDING_READBACK: str = (
    "Sliver fan-out has NOT yet run, so no storage node has confirmed "
    "this blob and Tx2 (certify_blob) cannot be submitted yet -- but Tx1 "
    "already succeeded and storage is already paid for, so reserve_space/"
    "register_blob does NOT need to be re-run. Resume the pipeline from "
    "sliver upload onward using this transaction's digest and object_id."
)


class RegistrationPendingError(RuntimeError):
    """Tx1 succeeded on-chain but its readback is not available yet.

    Raised when checkpoint finality or the subsequent object readback
    has not caught up with an already-successful ``reserve_space``/
    ``register_blob`` transaction. The condition is transient, not a
    lost transaction: ``digest`` and ``object_id`` are valid and can be
    used to resume the pipeline from sliver upload onward without
    re-executing Tx1.
    """

    def __init__(self, *, digest: str, object_id: str, detail: str) -> None:
        """Build the error with recovery attributes attached.

        Args:
            digest (str): Tx1's transaction digest.
            object_id (str): The Blob object id created by Tx1.
            detail (str): Description of the specific readback step that failed.
        """
        self.digest = digest
        self.object_id = object_id
        super().__init__(
            f"{detail} Tx1 (digest {digest}) SUCCEEDED on-chain -- the "
            f"blob is registered and storage is already paid for "
            f"(object_id {object_id}). {_RESUME_HINT_PENDING_READBACK}"
        )
"""Recovery guidance for a Tx1 read-back failure.

Deliberately steers AWAY from ``tusky certify_blob``. That command resumes
the confirmation-collection and Tx2 stages only, which is valid solely for
a failure AFTER ``upload_slivers``. A read-back failure happens inside the
register stage, before any sliver leaves the client, so pointing a caller
at ``certify_blob`` sends them at a command that is structurally incapable
of succeeding.
"""


async def resolve_package_id(*, client: WalrusClient, system_object: str) -> str:
    """Read the current Walrus package ID from the configured System object.

    ``pytusk.tusky.tusky_cmds``'s ``_walrus_package_id`` delegates to this
    function directly; the same pattern is also used by
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


@dataclasses.dataclass(kw_only=True, frozen=True)
class Registration:
    """The recoverable checkpoint between Tx1 (``reserve_space`` +
    ``register_blob``) and Tx2 (``certify_blob``).

    Holding a ``Registration`` is sufficient to resume the native upload flow
    at the confirmation-collection stage without re-encoding the blob or
    re-paying for storage/registration -- the expensive, WAL-spending work is
    already done once this is returned.

    There is deliberately NO ``storage_object_id`` field. ``reserve_space``
    returns its ``Storage`` result BY VALUE, and in the composed Tx1 PTB that
    result flows directly into ``register_blob``, which consumes it by value
    (see :func:`execute_reserve_and_register`). The ``Storage`` is never
    transferred to an address and never becomes an independently addressable
    object -- it ends up wrapped inside the created ``Blob`` object's
    ``storage`` field. There is therefore no standalone Storage object ID to
    report, and adding one back would imply a capability the caller does not
    have.

    Attributes:
        object_id (str): Object ID of the ``Blob`` created by
            ``register_blob`` in Tx1.
        blob_id (bytes): Raw 32-byte Walrus blob ID (matches
            ``EncodedBlob.blob_id``).
        end_epoch (int): The blob's storage expiration epoch, as read back
            from the created ``Blob`` object after Tx1.
        deletable (bool): Whether the registered blob is deletable.
        digest (str): Tx1's transaction digest, kept for diagnostics and
            manual recovery if a caller needs to inspect Tx1 independently
            of the fields already captured here.
    """

    object_id: str
    blob_id: bytes
    end_epoch: int
    deletable: bool
    digest: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class CertifyResult:
    """The outcome of submitting Tx2 (``certify_blob``).

    ``certified`` is always ``True`` when a ``CertifyResult`` is returned:
    :func:`execute_certify` raises on any on-chain abort or submission
    failure rather than returning a result with ``certified=False``. The
    field is kept explicit (rather than folded away) to mirror the shape of
    :class:`Registration` and to give callers an unambiguous field to log
    without inspecting exception state.

    Attributes:
        object_id (str): Object ID of the now-certified ``Blob``.
        blob_id (bytes): Raw 32-byte Walrus blob ID.
        certified (bool): Always ``True`` for a returned ``CertifyResult``.
        digest (str): Tx2's transaction digest.
    """

    object_id: str
    blob_id: bytes
    certified: bool
    digest: str


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


def _matches_wal_coin_type(*, coin_type: str, wal_coin_type: str) -> bool:
    """Check whether a coin_type string identifies the WAL coin.

    Ported field-for-field from ``_matches_wal_coin_type`` in
    ``pytusk.tusky.tusky_cmds`` (not imported from there -- ``tusky`` depends
    on ``client``, not the reverse). Uses an exact match against
    ``wal_coin_type`` when the active network has a pinned value (currently
    mainnet only); falls back to a substring match when unpinned (e.g.
    testnet, whose contracts are redeployed and don't have a stable package
    address to pin against).

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
    ``pytusk.tusky.tusky_cmds``: resolve the owner's WAL ``coin_type`` via a
    balance listing (matched via :func:`_matches_wal_coin_type`, the same
    pinned-exact/substring-fallback logic ``tusky_cmds`` uses), then list
    owned coins of that type via ``GetCoins`` and take the largest by
    balance. No merge logic is added -- ``reserve_space``/``register_blob``
    each deduct what they need from a single ``&mut Coin<WAL>`` and leave the
    remainder in place, matching how ``system::extend_blob`` payment already
    works.

    This is a FALLBACK ONLY: an SDK developer composing Tx1 directly via
    :func:`add_reserve_and_register` supplies ``payment_coin`` themselves
    (they know their own wallet's WAL coin type without a network round
    trip). This helper exists so :func:`execute_reserve_and_register` can
    still select one automatically when ``payment_coin`` is omitted, for
    backward compatibility with callers of the old one-call behaviour.

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


# Private alias retained for internal callers within this module and for
# existing test imports; select_wal_payment_coin is the public name.
_select_wal_payment_coin = select_wal_payment_coin


def _end_epoch_and_deletable(obj: sui_prot.Object) -> tuple[int, bool]:
    """Extract a freshly created Blob object's storage end_epoch and deletable flag.

    Ported field-for-field from ``_blob_deletable_and_end_epoch`` in
    ``pytusk.tusky.tusky_cmds`` (not imported from there -- ``tusky`` depends
    on ``client``, not the reverse).

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.

    Returns:
        tuple[int, bool]: (end_epoch, deletable).

    Raises:
        ValueError: If the object's JSON view is missing fields a Walrus
            Blob object is expected to have (e.g. an incomplete RPC
            response).
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id} has no JSON view; cannot determine "
            "deletable/end_epoch."
        )
    fields = obj.json.struct_value.fields
    storage_val = fields.get("storage")
    if not (storage_val and storage_val.struct_value):
        raise ValueError(
            f"Object {obj.object_id} is missing its 'storage' field; "
            "cannot determine end_epoch."
        )
    end_epoch_val = storage_val.struct_value.fields.get("end_epoch")
    if end_epoch_val is None:
        raise ValueError(
            f"Object {obj.object_id}'s storage field is missing 'end_epoch'."
        )
    end_epoch = int(end_epoch_val.number_value or 0)
    deletable_val = fields.get("deletable")
    if deletable_val is None:
        raise ValueError(f"Object {obj.object_id} is missing its 'deletable' field.")
    deletable = bool(deletable_val.bool_value)
    return end_epoch, deletable


def find_created_object_id(
    *, effects: sui_prot.TransactionEffects, owner: str
) -> str:
    """Find the object ID of the single object created and owned by ``owner``.

    Public so a caller who now owns transaction submission (per this
    module's caller-owns-the-transaction model -- see the module docstring)
    and holds their own ``TransactionEffects`` can locate a PTB's created
    object without reimplementing this lookup.

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


# Private alias retained for internal callers within this module and for
# existing test imports; find_created_object_id is the public name.
_find_created_object_id = find_created_object_id


def _require_success(
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
    same mechanism ``delete_blob`` and ``exchange_for_wal`` in
    ``pytusk.tusky.tusky_cmds`` already use for their own command results.

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

    ``storage_amount`` is computed via :func:`_encoded_storage_amount`,
    which ports ``encoded_blob_length`` from ``redstuff.move`` -- the MOVE
    version is authoritative since ``reserve_space``'s ``storage_amount`` is
    charged on-chain against Move's own computation.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add move_calls to.
        package_id (str): Walrus package ID, as resolved by
            :func:`resolve_package_id`.
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
    :func:`~pytusk.core.system_ops.execute_reserve_and_register` (Tx1) is
    the reason this is necessary: it always transfers the newly registered
    ``Blob`` to the resolved sender, never to an arbitrary recipient, so
    that the sender still owns (and can sign for) the object when Tx2
    certifies it.

    Args:
        txn (AsyncSuiTransaction): The caller's already-created transaction
            to add the move_call to.
        package_id (str): Walrus package ID, as resolved by
            :func:`resolve_package_id`.
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


async def execute_reserve_and_register(
    *,
    client: WalrusClient,
    encoded: EncodedBlob,
    epochs: int,
    deletable: bool,
    package_id: str,
    system_object: str,
    payment_coin: str | None = None,
    sender: str | None = None,
    sponsor: str | None = None,
    finality_max_attempts: int = DEFAULT_FINALITY_MAX_ATTEMPTS,
    finality_max_delay: float = DEFAULT_FINALITY_MAX_DELAY,
) -> Registration:
    """Build and execute Tx1: ``reserve_space`` composed with ``register_blob``.

    THIN WRAPPER around :func:`add_reserve_and_register` -- see the module
    docstring's "caller owns the transaction lifecycle" note. All PTB
    composition lives in :func:`add_reserve_and_register`; this function
    only opens a transaction, delegates to it, consumes the returned
    ``Blob`` result (transferring it to the resolved ``sender`` -- an
    object result left unconsumed by a later command would otherwise abort
    the PTB, exactly the reason ``delete_blob`` and ``exchange_for_wal`` in
    ``pytusk.tusky.tusky_cmds`` transfer their own move_call results), then
    builds, signs, and submits it. It exists for a caller who wants the old
    one-call behaviour rather than composing Tx1 by hand.

    The ``Blob`` is ALWAYS transferred to the resolved ``sender`` -- there
    is deliberately no way to hand it to a different recipient here.
    ``certify_blob`` (Tx2) takes the blob as ``&mut Blob`` and must be
    signed by its current owner, so if Tx1 transferred the object to a
    third-party recipient, Tx2 would fail at simulation with "Transaction
    was not signed by the correct sender" once ``certify_blob`` tried to
    mutate an object it no longer owns. Handing the ``Blob`` to a different
    recipient is Tx2's job: pass ``recipient`` to
    :func:`execute_certify`/:func:`~pytusk.core.system_ops.add_certify`
    instead, which transfers it in the SAME PTB immediately after
    certification, so the sender still owns it when it is signed for and
    the eventual transfer is atomic with certification succeeding.

    ``payment_coin`` is OPTIONAL here (unlike the required argument on
    :func:`add_reserve_and_register`): when omitted, it falls back to
    :func:`select_wal_payment_coin` for backward compatibility with
    callers of the old one-call behaviour. An SDK developer who knows their
    own wallet's WAL coin type should pass ``payment_coin`` explicitly and
    skip that extra network round trip.

    ``sender``/``sponsor`` are threaded into
    ``client.transaction(initial_sender=..., initial_sponsor=...)``,
    matching how ``burn_blob`` in ``pytusk.tusky.tusky_cmds`` resolves and
    passes them. When omitted, ``sender`` defaults to the active address and
    no sponsor is used -- the same behaviour as the previous bare
    ``client.transaction()`` call.

    Gas is left to pysui's automatic simulate, matching every existing PTB
    builder in this repo (none pass an explicit budget).

    Args:
        client (WalrusClient): Client used to query coins and submit the
            transaction.
        encoded (EncodedBlob): The RedStuff-encoded blob to register.
        epochs (int): Number of epochs ahead to reserve storage for
            (``epochs_ahead`` on ``reserve_space``).
        deletable (bool): Whether the registered blob should be deletable.
        package_id (str): Walrus package ID, as resolved by
            :func:`resolve_package_id`.
        system_object (str): Object ID of the configured Walrus System
            object.
        payment_coin (str | None): Object ID of a ``Coin<WAL>`` to use as
            payment. When ``None``, one is selected automatically via
            :func:`select_wal_payment_coin`.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Always the owner of the created ``Blob``
            -- see the transfer note above.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.
        finality_max_attempts (int): Poll budget handed to
            :func:`wait_for_finality` before the read-back. Defaults to
            :data:`DEFAULT_FINALITY_MAX_ATTEMPTS`.
        finality_max_delay (float): Per-attempt delay ceiling, in seconds,
            handed to :func:`wait_for_finality`. Defaults to
            :data:`DEFAULT_FINALITY_MAX_DELAY`.

    Returns:
        Registration: The recoverable checkpoint for Tx2.

    Raises:
        RuntimeError: If WAL coin selection, transaction submission, or
            on-chain execution fails. If Tx1 SUCCEEDS on-chain but
            :func:`find_created_object_id` cannot locate the created
            ``Blob`` in the effects (an assumption UNVERIFIED against a
            live node -- see that function's docstring), the error is
            re-raised (chained via ``from``) with Tx1's transaction digest
            and :data:`_RESUME_HINT` appended. If Tx1 SUCCEEDS and the
            created ``Blob``'s object ID IS found, but the subsequent
            convenience read-back fails -- either :class:`GetObject`
            returns not-ok, or :func:`_end_epoch_and_deletable` raises
            ``ValueError`` on a malformed/unexpected JSON view (the latter
            chained via ``from``) -- the error carries Tx1's transaction
            digest and the same hint.

            The read-back is preceded by :func:`wait_for_finality`, so a
            failure here is a genuine anomaly rather than the pre-checkpoint
            race that previously produced a stub ``Object`` with no
            ``object_id``. Note that NONE of these failures are recoverable
            with ``tusky certify_blob`` -- see :data:`_RESUME_HINT`.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    resolved_payment_coin = payment_coin or await select_wal_payment_coin(
        client=client, owner=resolved_sender
    )

    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    blob = await add_reserve_and_register(
        txn=txn,
        package_id=package_id,
        system_object=system_object,
        encoded=encoded,
        epochs=epochs,
        deletable=deletable,
        payment_coin=resolved_payment_coin,
    )
    await txn.transfer_objects(transfers=[blob], recipient=resolved_sender)
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(
            f"reserve_space/register_blob transaction failed: {result.result_string}"
        )
    effects = _require_success(
        result_data=result.result_data, label="reserve_space/register_blob"
    )

    try:
        object_id = find_created_object_id(effects=effects, owner=resolved_sender)
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc} Tx1 (digest {result.result_data.digest}) SUCCEEDED "
            "on-chain -- the blob is registered and storage is already "
            "paid for, so this is a lookup failure, not a lost "
            f"transaction. {_RESUME_HINT}"
        ) from exc

    finalized = await wait_for_finality(
        client=client,
        digest=result.result_data.digest,
        max_attempts=finality_max_attempts,
        max_delay=finality_max_delay,
    )
    if not finalized:
        raise RegistrationPendingError(
            digest=result.result_data.digest,
            object_id=object_id,
            detail=(
                "Checkpoint finality was not reached after "
                f"{finality_max_attempts} attempts."
            ),
        )

    blob_result = await client.execute(command=GetObject(object_id=object_id))
    if not blob_result.is_ok():
        raise RegistrationPendingError(
            digest=result.result_data.digest,
            object_id=object_id,
            detail=f"Cannot fetch newly created Blob: {blob_result.result_string}",
        )
    try:
        end_epoch, actual_deletable = _end_epoch_and_deletable(blob_result.result_data)
    except ValueError as exc:
        raise RuntimeError(
            f"{exc} Tx1 (digest {result.result_data.digest}) SUCCEEDED "
            "on-chain -- the blob is registered and storage is already "
            "paid for, so this is only a read-back failure, not a lost "
            f"transaction. {_RESUME_HINT}"
        ) from exc

    return Registration(
        object_id=object_id,
        blob_id=encoded.blob_id,
        end_epoch=end_epoch,
        deletable=actual_deletable,
        digest=result.result_data.digest,
    )


async def execute_certify(
    *,
    client: WalrusClient,
    registration: Registration,
    certificate: Certificate,
    package_id: str,
    system_object: str,
    sender: str | None = None,
    sponsor: str | None = None,
    recipient: str | None = None,
) -> CertifyResult:
    """Build and execute Tx2: ``certify_blob``.

    THIN WRAPPER around :func:`add_certify` -- see the module docstring's
    "caller owns the transaction lifecycle" note. All PTB composition lives
    in :func:`add_certify`; this function only opens a transaction,
    delegates to it, then builds, signs, and submits it. ``certify_blob``
    has no return value (it mutates the referenced ``Blob`` in place), so
    unlike Tx1 there is no command result to consume/transfer -- unless
    ``recipient`` is given, in which case :func:`add_certify` appends a
    ``transfer_objects`` command after ``certify_blob`` in the same PTB
    (see its docstring).

    ``sender``/``sponsor`` are threaded into
    ``client.transaction(initial_sender=..., initial_sponsor=...)``,
    matching how ``burn_blob`` in ``pytusk.tusky.tusky_cmds`` resolves and
    passes them. When omitted, ``sender`` defaults to the active address and
    no sponsor is used -- the same behaviour as the previous bare
    ``client.transaction()`` call.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        registration (Registration): Tx1's result, identifying the Blob to
            certify.
        certificate (Certificate): The quorum-backed certificate to submit.
        package_id (str): Walrus package ID, as resolved by
            :func:`resolve_package_id`.
        system_object (str): Object ID of the configured Walrus System
            object.
        sender (str | None): Address to sign the transaction as. Defaults
            to the active address when ``None``. Must be the current owner
            of the ``Blob`` being certified -- see
            :func:`execute_reserve_and_register`'s docstring for why Tx1
            always transfers it there.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.
        recipient (str | None): Sui address to transfer the now-certified
            ``Blob`` to, in the same PTB, immediately after
            ``certify_blob``. Passed straight through to
            :func:`add_certify`. When ``None`` (the default), no transfer
            is added and the ``Blob`` stays with ``sender``.

    Returns:
        CertifyResult: The outcome of certification.

    Raises:
        RuntimeError: If transaction submission or on-chain execution fails.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_certify(
        txn=txn,
        package_id=package_id,
        system_object=system_object,
        blob_object_id=registration.object_id,
        certificate=certificate,
        recipient=recipient,
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"certify_blob transaction failed: {result.result_string}")
    _require_success(result_data=result.result_data, label="certify_blob")

    return CertifyResult(
        object_id=registration.object_id,
        blob_id=registration.blob_id,
        certified=True,
        digest=result.result_data.digest,
    )
