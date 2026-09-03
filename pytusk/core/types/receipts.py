#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Timing and receipt dataclasses for the native upload and relay upload
pipelines.

Pure result types with no behaviour beyond their own fields -- callers
branch on the fields (e.g. :class:`RelayBlobReceipt`'s ``outcome``) rather
than on exceptions raised out of these pipelines.
"""

import dataclasses

from dataclasses_json import DataClassJsonMixin

from pytusk.core.types.outcomes import RelayOutcome, RelayUploadOutcome
from pytusk.core.types.quilts import QuiltPatchReceipt


@dataclasses.dataclass(kw_only=True, frozen=True)
class StageTimings:
    """Wall-clock duration, in seconds, of each native upload pipeline stage.

    Captured via :func:`time.monotonic` (never wall/civil time -- see
    :func:`upload_slivers`'s docstring for the same reasoning), so these
    figures are immune to clock adjustments and safe to difference.

    Every field is ``float | None``: ``None`` is a real, meaningful value
    meaning the stage never ran -- e.g. ``tusky store_blob_native --mode
    simulate`` stops after Tx1, and a failed pipeline run stops at whatever
    stage broke. A stage that did not run must never be reported as
    ``0.0``, which would be indistinguishable from "ran and took no time".

    A stage's duration is recorded even when that stage FAILS: callers that
    populate this dataclass do so from a ``try``/``finally`` around the
    stage's call, so a sliver fan-out that ran for 40 minutes before dying
    still reports that 40 minutes here.

    Attributes:
        encode (float | None): Duration of the ``encode_blob`` call.
        register_tx1 (float | None): Duration of
            :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register`
            (Tx1: ``reserve_space`` + ``register_blob``).
        sliver_upload (float | None): Duration of :func:`upload_slivers`.
        confirmations (float | None): Duration of
            :func:`collect_confirmations`.
        certify_tx2 (float | None): Duration of the ``execute_certify``
            call inside :func:`certify` (Tx2: ``certify_blob``).
        total (float | None): Total wall-clock duration of the whole
            pipeline invocation that produced these timings, from its own
            entry point to its return -- not merely the sum of the other
            fields, since it also captures unattributed overhead between
            stages (e.g. committee/package-ID reads).
    """

    encode: float | None
    register_tx1: float | None
    sliver_upload: float | None
    confirmations: float | None
    certify_tx2: float | None
    total: float | None


@dataclasses.dataclass(kw_only=True, frozen=True)
class NativeBlobReceipt:
    """The outcome of a native upload attempt.

    There is deliberately NO ``storage_object_id`` field. The ``Storage``
    returned by ``reserve_space`` is consumed BY VALUE inside Tx1 and
    wrapped into the created ``Blob`` object -- it is never transferred to
    an address and never becomes an independently addressable object (see
    :class:`~pytusk.core.types.receipts.Registration`'s docstring for the
    same reasoning). There is therefore no standalone Storage object ID to
    report here either; do not re-add this field.

    Attributes:
        blob_id (str): The blob ID as URL-safe, unpadded base64 (see
            :func:`~pytusk.core.encoding.blob_id_to_url_base64`).
        object_id (str): Object ID of the ``Blob`` created by
            ``register_blob``.
        certified (bool): True once ``certify_blob`` has succeeded.
        end_epoch (int | None): The blob's storage expiration epoch.
            ``None`` on the one receipt path where it is genuinely
            unavailable: a :class:`~pytusk.core.types.errors.RegistrationPendingError`
            caught at the pipeline boundary, where Tx1 succeeded on-chain
            but the ``Registration`` read-back never completed, so there is
            no real value to report -- see
            :func:`~pytusk.core.pipelines.write.store_blob_native`.
        failed_stage (str | None): Name of the pipeline stage that failed,
            when this receipt represents a partial/failed attempt; ``None``
            on a fully certified upload.
        timings (StageTimings): Wall-clock duration of each pipeline stage
            that ran before this receipt was produced -- populated on both
            the fully-certified and the partial/failed path (see
            :class:`StageTimings`).
        register_tx_digest (str | None): Digest of Tx1 (``reserve_space`` +
            ``register_blob``). Defaults to ``None`` so existing
            construction sites do not all need to change at once; populated
            on both the fully-certified and the partial/failed path, since
            Tx1 having succeeded is exactly what makes a partial/failed
            receipt worth reporting.
        certify_tx_digest (str | None): Digest of Tx2 (``certify_blob``).
            Defaults to ``None`` for the same reason as
            ``register_tx_digest``, and stays ``None`` on any receipt where
            Tx2 never completed successfully.
    """

    blob_id: str
    object_id: str
    certified: bool
    end_epoch: int | None
    failed_stage: str | None
    timings: StageTimings
    register_tx_digest: str | None = None
    certify_tx_digest: str | None = None


@dataclasses.dataclass(kw_only=True, frozen=True)
class RelayStageTimings:
    """Wall-clock duration, in seconds, of each relay upload stage.

    Captured via :func:`time.monotonic` (never wall/civil time), so these
    figures are immune to clock adjustments and safe to difference.

    Every field is ``float | None``: ``None`` is a real, meaningful value
    meaning the stage never ran -- e.g. ``tusky store_blob_relay --mode
    simulate`` stops after Tx1, and a failed run stops at whatever stage
    broke. A stage that did not run must never be reported as ``0.0``,
    which would be indistinguishable from "ran and took no time".

    A stage's duration is recorded even when that stage FAILS.

    Attributes:
        encode (float | None): Duration of the ``encode_blob`` call.
        tip_config (float | None): Duration of the ``GET /v1/tip-config``
            request.
        register_tip_tx (float | None): Duration of Tx1. This is ONE stage
            because the registration and the tip are deliberately bundled
            into a single PTB.
        relay_upload (float | None): Duration of the ``POST
            /v1/blob-upload-relay`` request, including any retries. Usually
            the dominant stage.
        certify_tx (float | None): Duration of Tx2 (``certify_blob``).
        total (float | None): Total wall-clock duration of the whole
            invocation, from its entry point to its return -- not merely
            the sum of the other fields, since it also captures
            unattributed overhead between stages (e.g. committee reads).
    """

    encode: float | None
    tip_config: float | None
    register_tip_tx: float | None
    relay_upload: float | None
    certify_tx: float | None
    total: float | None


@dataclasses.dataclass(kw_only=True, frozen=True)
class RelayBlobReceipt:
    """The outcome of a relay upload attempt.

    This is a RESULT type, not an exception. Every operational outcome --
    including a paid tip whose upload never completed -- is reported here.
    Exceptions are reserved for contract violations by the caller.

    Which fields are populated depends on :attr:`outcome`: ``nonce`` is set
    when the outcome is :attr:`RelayOutcome.RESUMABLE`, and
    ``relay_status``/``relay_message`` when it is
    :attr:`RelayOutcome.REJECTED`.

    Attributes:
        outcome (RelayOutcome): Terminal state of the attempt.
        blob_id (str | None): Blob ID as URL-safe, unpadded base64.
        object_id (str | None): Object ID of the ``Blob`` created by
            ``register_blob``.
        certified (bool): True once ``certify_blob`` has succeeded.
        end_epoch (int | None): The blob's storage expiration epoch.
        failed_stage (str | None): Name of the stage that failed; ``None``
            on a fully certified upload.
        timings (RelayStageTimings): Duration of each stage that ran.
        relay_url (str): Base URL of the relay this attempt used.
        tip_paid (bool): True once Tx1 executed and the tip transferred.
        tip_amount (int | None): Tip in MIST, or ``None`` for a ``no_tip``
            relay or an attempt that failed before quoting.
        register_tx_digest (str | None): Digest of Tx1 (the bundled
            ``reserve_space`` + ``register_blob`` + tip). Doubles as the
            ``tx_id`` a resumed POST must send.
        certify_tx_digest (str | None): Digest of Tx2 (``certify_blob``).
        nonce (str | None): Base64url nonce, populated when the outcome is
            :attr:`RelayOutcome.RESUMABLE`, so a later POST can reuse the
            tip already paid.
        relay_status (int | None): HTTP status, populated when the outcome
            is :attr:`RelayOutcome.REJECTED`.
        relay_message (str | None): Relay response body, verbatim,
            populated when the outcome is :attr:`RelayOutcome.REJECTED`.
            The body is what separates a refusal from a missing answer, so
            it is preserved rather than summarised.
        attempts (int | None): POSTs actually made against the relay.
            ``None`` -- never ``0`` -- when the upload stage was never
            reached, matching :class:`RelayStageTimings`' convention that
            ``None`` means "did not run".
        transport_error (str | None): Last transport-level failure, when
            the relay never answered at all. This is what separates one
            dropped connection from a budget spent on five, and it has no
            HTTP status to report alongside it.
    """

    outcome: RelayOutcome
    blob_id: str | None
    object_id: str | None
    certified: bool
    end_epoch: int | None
    failed_stage: str | None
    timings: RelayStageTimings
    relay_url: str
    tip_paid: bool
    tip_amount: int | None
    register_tx_digest: str | None
    certify_tx_digest: str | None
    nonce: str | None
    relay_status: int | None
    relay_message: str | None
    attempts: int | None
    transport_error: str | None


@dataclasses.dataclass(kw_only=True, frozen=True)
class QuiltRelayReceipt(RelayBlobReceipt):
    """The outcome of a relay upload that stored a QUILT.

    A quilt IS an ordinary blob on chain, so every inherited field means
    exactly what it means on the blob path -- ``blob_id`` IS the quilt id,
    and no field here carries meaning only on some other path. That is what
    makes subclassing honest here rather than padding.

    What a quilt ADDS is addressability: the per-patch identities a reader
    needs in order to fetch one packed blob back out without downloading the
    whole quilt.

    Satisfies :class:`~pytusk.core.types.protocols.UploadReceipt`
    covariantly, exactly as its base does.

    Attributes:
        patches (tuple[QuiltPatchReceipt, ...]): One entry per packed blob,
            carrying its identifier, tags, column range and ``QuiltPatchId``.
            Ordered as the assembled quilt orders them -- SORTED BY
            IDENTIFIER, not the caller's input order. Required rather than
            defaulted: assembly is PRE-SPEND, so a failure early enough to
            have produced no layout raises instead of returning a receipt.
    """

    patches: tuple[QuiltPatchReceipt, ...]


@dataclasses.dataclass(kw_only=True, frozen=True)
class RelayUploadResult:
    """Outcome of the POST stage: the certificate, or the recovery points.

    The composable twin of :class:`RelayBlobReceipt`, and deliberately the
    same shape of thing -- frozen, keyword-only, an explicit ``outcome``
    discriminator first, then the success payload and every fact needed to
    recover. Values that also appear on the receipt keep the receipt's
    names, so an SDK user moving between the composable and encapsulated
    layers does not re-learn the vocabulary for identical data.

    Like the receipt, this REPORTS failure rather than raising it. A caller
    must branch on :attr:`outcome`, not on whether
    :attr:`certificate` happens to be set.

    Attributes:
        outcome (RelayUploadOutcome): Terminal state of the POST stage.
        certificate (dict | None): The relay's confirmation certificate as
            raw JSON, present only when the outcome is
            :attr:`RelayUploadOutcome.UPLOADED`. Left unparsed here --
            :func:`~pytusk.core.relay_upload.relay_certify.parse_relay_certificate`
            converts it, because its three parts are encoded
            inconsistently.
        blob_id (str): Blob ID posted, as URL-safe unpadded base64. A
            resumption token.
        relay_url (str): Base URL of the relay actually posted to.
        register_tip_tx_digest (str | None): Digest of Tx1, sent as the
            relay's ``tx_id`` query parameter. ``None`` when the relay
            required no tip. A resumption token.
        nonce (str | None): Base64url nonce sent with the POST. ``None``
            when the relay required no tip. A resumption token.
        relay_status (int | None): HTTP status, when the relay answered.
        relay_message (str | None): Response body, verbatim, when the relay
            answered. Only the body distinguishes which 400 occurred.
        transport_error (str | None): Last transport-level failure, when
            the relay never answered.
        attempts (int): POSTs actually made, always at least one.
        duration (float): Wall-clock seconds across every attempt.
    """

    outcome: RelayUploadOutcome
    certificate: dict | None
    blob_id: str
    relay_url: str
    register_tip_tx_digest: str | None
    nonce: str | None
    relay_status: int | None
    relay_message: str | None
    transport_error: str | None
    attempts: int
    duration: float


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
    (see :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register`).
    The ``Storage`` is never transferred to an address and never becomes an
    independently addressable object -- it ends up wrapped inside the
    created ``Blob`` object's ``storage`` field. There is therefore no
    standalone Storage object ID to report, and adding one back would imply
    a capability the caller does not have.

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
    :func:`~pytusk.core.ops.blob_execute.execute_certify` raises on any
    on-chain abort or submission failure rather than returning a result
    with ``certified=False``. The field is kept explicit (rather than
    folded away) to mirror the shape of :class:`Registration` and to give
    callers an unambiguous field to log without inspecting exception state.

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


@dataclasses.dataclass(kw_only=True, frozen=True)
class StorageOpResult:
    """Outcome of a storage operation that creates no new object.

    Returned by :func:`~pytusk.core.ops.storage_execute.execute_fuse` and
    :func:`~pytusk.core.ops.storage_execute.execute_destroy_storage`. Both
    Move calls (``fuse``, ``destroy``) have no return value, so there is no
    created object to report -- only which storage was acted on and the
    transaction that did it.

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
class BlobMetadataOpResult:
    """Outcome of a Blob metadata operation.

    Returned by :func:`~pytusk.core.ops.blob_metadata_execute.execute_set_blob_metadata`,
    :func:`~pytusk.core.ops.blob_metadata_execute.execute_drop_blob_metadata_keys`,
    and :func:`~pytusk.core.ops.blob_metadata_execute.execute_drop_blob_metadata_all`.
    Mirrors :class:`StorageOpResult`'s shape for the same reason: none of
    ``insert_or_update_metadata_pair``, ``remove_metadata_pair``, or
    ``take_metadata`` return anything a caller needs to consume, so there is
    no created object to report -- only which ``Blob`` was acted on and the
    transaction that did it.

    Attributes:
        object_id (str): Object ID of the ``Blob`` whose metadata was
            written or removed.
        digest (str): The transaction digest.
    """

    object_id: str
    digest: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class SplitResult:
    """Outcome of splitting a ``Storage`` object.

    Returned by :func:`~pytusk.core.ops.storage_execute.execute_split_by_epoch`
    and :func:`~pytusk.core.ops.storage_execute.execute_split_by_size`.
    Unlike :class:`StorageOpResult`, a split DOES create a new object, and
    its ID is the value the caller most likely needs next.

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
