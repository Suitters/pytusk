#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Exception types raised by the native upload and relay upload pipelines.

These are pure exception hierarchies with no dependency on either
``pytusk.core.native_upload`` or ``pytusk.core.relay_upload`` -- both of
those packages import from here, never the other way round.
"""


class ConfirmationMismatchError(ValueError):
    """Raised when storage nodes returned differing confirmation messages.

    The client passes exactly ONE node's ``serialized_message`` bytes,
    verbatim, into ``certify_blob`` -- see
    :class:`~pytusk.core.certification.NodeConfirmation`. If the
    nodes being certified together do not agree on that message, there is no
    single message the resulting certificate can be meaningful against, and
    building one must fail before any signature work is wasted on it.
    """


class InvalidConfirmationError(ValueError):
    """Raised when a node's signature fails verification against its key.

    This means the storage node's committee public key, as resolved by the
    caller, does not authenticate the signature it returned over the expected
    confirmation message.
    """


class QuorumNotReachedError(RuntimeError):
    """Raised when accumulated signer weight is below the quorum threshold."""


class NativeUploadError(RuntimeError):
    """Base error for a failed stage of the native upload pipeline.

    Attributes:
        stage (str): Name of the pipeline stage that failed (e.g.
            ``"upload_slivers"``, ``"collect_confirmations"``,
            ``"certify"``).
        duration (float | None): Wall-clock duration, in seconds, of the
            raising site's own timed work before it failed, when the
            raising site tracked one and chose to report it; ``None``
            otherwise (e.g. a failure discovered before that stage's timed
            work began, or a stage whose duration
            :func:`store_blob_native` already tracks itself via its own
            local ``try``/``finally`` and therefore does not need repeated
            here).
    """

    stage: str
    duration: float | None

    def __init__(
        self, *, message: str, stage: str, duration: float | None = None
    ) -> None:
        """Initialise with a human-readable message, the failing stage, and
        an optional stage duration.

        Args:
            message (str): Human-readable description of the failure.
            stage (str): Name of the pipeline stage that failed.
            duration (float | None): Wall-clock duration, in seconds, of
                the raising site's own timed work before it failed, if
                tracked; ``None`` when not applicable.
        """
        super().__init__(message)
        self.stage = stage
        self.duration = duration


class SliverUploadError(NativeUploadError):
    """Raised when sliver fan-out failed to reach quorum weight."""


class ConfirmationCollectionError(NativeUploadError):
    """Raised when the confirmation-collection stage fails.

    Covers a quorum of storage-node confirmations not being gathered, and
    also wraps :class:`ConfirmationMismatchError` and
    :class:`InvalidConfirmationError` when
    :func:`~pytusk.core.certification.build_certificate` rejects the
    collected confirmations -- see :func:`collect_confirmations`'s
    docstring.
    """


class EpochMismatchError(NativeUploadError):
    """Raised when the on-chain epoch kept moving and certification retries
    were exhausted."""


class CertifyTransactionError(NativeUploadError):
    """Raised when Tx2 (``certify_blob``) fails to submit, aborts on-chain,
    fails pysui's pre-submission gas-estimation dry run, or when local
    certificate verification rejects the certificate before Tx2 is even
    attempted.

    Wraps TWO different bare exception types that
    :func:`~pytusk.core.ops.blob_execute.execute_certify` (and the pysui
    machinery it calls) can raise: a bare ``RuntimeError`` on a submission
    failure or an on-chain abort, and a bare ``ValueError`` when pysui's
    gas-estimation dry run inside ``txn.build_and_sign()`` fails (pysui's
    ``txn_gas.py`` raises ``ValueError(f"Error running
    SimulateTransactionKind: {result.result_string}")`` in that case, not
    ``RuntimeError``). See :func:`certify`'s docstring for the full
    ``Raises`` note. ``pytusk.core.ops`` deliberately does not depend on
    ``native_upload`` (see that module's docstring), so the conversion from
    either bare exception type to this :class:`NativeUploadError` subclass
    happens here, on the ``native_upload`` side of that boundary, inside
    :func:`certify` -- not in ``execute_certify`` itself. The original error
    text is preserved verbatim as this exception's message.
    """


class RelayUploadError(RuntimeError):
    """Base error for a failed stage of the relay upload pipeline.

    Raised only for contract violations and unrecoverable local faults.
    Operational outcomes -- a refused upload, an unfinished POST -- are
    reported through :class:`~pytusk.core.types.RelayBlobReceipt` instead,
    so that a paid tip is never hidden inside a traceback.

    Attributes:
        stage (str): Name of the pipeline stage that failed (e.g.
            ``"fetch_tip_config"``, ``"add_tip"``, ``"upload_to_relay"``).
        duration (float | None): Wall-clock duration, in seconds, of the
            raising site's own timed work before it failed, when the
            raising site tracked one; ``None`` otherwise.
    """

    stage: str
    duration: float | None

    def __init__(
        self, *, message: str, stage: str, duration: float | None = None
    ) -> None:
        """Initialise with a human-readable message, the failing stage, and
        an optional stage duration.

        Args:
            message (str): Human-readable description of the failure.
            stage (str): Name of the pipeline stage that failed.
            duration (float | None): Wall-clock duration, in seconds, of
                the raising site's own timed work before it failed, if
                tracked; ``None`` when not applicable.
        """
        super().__init__(message)
        self.stage = stage
        self.duration = duration


class TipCeilingExceededError(RelayUploadError):
    """Raised when the relay's quoted tip exceeds the caller's ``max_tip``.

    A refusal, not a failure. The relay answered correctly and the payer
    may well be able to afford the tip -- the caller simply declined the
    price. :func:`~pytusk.store_blob_relay` raises this as soon as the
    quote comes back, before the payment pre-flight and before any PTB is
    composed, so nothing is signed, submitted, or spent. Pre-spend, so it
    raises rather than reporting the outcome on a
    :class:`~pytusk.core.types.RelayBlobReceipt`.

    Distinct from :class:`TipPaymentError`, which means composing or
    paying the tip went wrong; here nothing went wrong. The quoted amount
    and the ceiling that rejected it are both carried in the message.
    """


class TipConfigError(RelayUploadError):
    """Raised when ``GET /v1/tip-config`` fails or returns a payload that
    does not match Walrus's ``TipConfig`` shape."""


class TipPaymentError(RelayUploadError):
    """Raised when the tip cannot be composed or paid.

    Covers the caller-side contract violations that must fail before any
    money moves: composing the tip into a transaction that already has
    inputs (the authentication package must be input 0), naming a sponsor
    absent from the active ``PysuiConfiguration``, and naming a
    ``tip_source`` coin owned by neither the sender nor the sponsor.
    """


class RelayCertificateParseError(RelayUploadError):
    """Raised when the relay's confirmation certificate cannot be parsed.

    The relay encodes the certificate's three parts INCONSISTENTLY:
    ``signers`` as a JSON integer array, ``serialized_message`` as a plain
    integer array (not base64), and ``signature`` as a base64 string. A
    payload that does not match that mixed shape raises this.
    """


class RelayCertifyTransactionError(RelayUploadError):
    """Raised when Tx2 (``certify_blob``) fails to submit, aborts on-chain,
    fails pysui's pre-submission gas-estimation dry run, or when local
    certificate verification rejects the certificate before Tx2 is even
    attempted -- the relay path's counterpart to
    :class:`CertifyTransactionError`.

    Both exceptions are produced by the same shared Tx2 stage,
    :func:`~pytusk.core.ops.blob_execute.submit_certification`, via its
    ``error_type`` parameter: the native path passes
    :class:`CertifyTransactionError` and the relay path passes this class,
    so each pipeline reports the same failure under its own name rather
    than the other's.
    """


_RESUME_HINT_PENDING_READBACK: str = (
    "Sliver fan-out has NOT yet run, so no storage node has confirmed "
    "this blob and Tx2 (certify_blob) cannot be submitted yet -- but Tx1 "
    "already succeeded and storage is already paid for, so reserve_space/"
    "register_blob does NOT need to be re-run. Resume the pipeline from "
    "sliver upload onward using this transaction's digest and object_id."
)


class ChainContextError(RuntimeError):
    """Raised when a pre-action chain read fails, naming which read failed.

    :func:`~pytusk.core.ops.system_reads.prepare_chain_context` serves the
    write pipelines, the read path and cost estimation alike, so it cannot
    report under any one of their error families. It raises this instead, and
    a caller that has its own family translates it, preserving ``stage``.

    Everything this covers is PRE-SPEND: nothing has been registered and no
    WAL has moved, so a failure has no on-chain state to report.

    Attributes:
        stage (str): Which read failed -- ``"committee"`` or
            ``"resolve_package_id"``.
    """

    def __init__(self, *, message: str, stage: str) -> None:
        """Initialize the error.

        Args:
            message (str): Human-readable description of the failure.
            stage (str): Which read failed.
        """
        super().__init__(message)
        self.stage = stage


class RegistrationPendingError(RuntimeError):
    """Tx1 succeeded on-chain but its readback is not available yet.

    Raised when checkpoint finality or the subsequent object readback
    has not caught up with an already-successful ``reserve_space``/
    ``register_blob`` transaction. The condition is transient, not a
    lost transaction: ``digest`` and ``object_id`` are valid and can be
    used to resume the pipeline from sliver upload onward without
    re-executing Tx1.
    """

    def __init__(self, *, digest: str, object_id: str, detail: str, stage: str) -> None:
        """Build the error with recovery attributes attached.

        Args:
            digest (str): Tx1's transaction digest.
            object_id (str): The Blob object id created by Tx1.
            detail (str): Description of the specific readback step that failed.
            stage (str): Which post-spend step failed -- ``"register_finality"``
                for a checkpoint-finality timeout, or ``"register_readback"``
                for a failed object read-back after finality landed.
        """
        self.digest = digest
        self.object_id = object_id
        self.stage = stage
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
