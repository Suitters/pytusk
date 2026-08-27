#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared types and errors used by two or more upload relay pipeline stages.

See :mod:`pytusk.core.relay_upload` (the package's ``__init__.py``) for the
full relay upload description -- stage ordering, why the tip and the
registration must share a single transaction, and why a failed POST is
reported as an outcome rather than raised.
"""

from __future__ import annotations

import base64
import dataclasses
import enum

from pytusk.core.relay_types import TipKind


class RelayOutcome(str, enum.Enum):
    """Terminal state of a relay upload attempt.

    The distinction between :attr:`RESUMABLE` and :attr:`REJECTED` is the
    load-bearing one. An HTTP status alone cannot separate "the relay never
    answered" from "the relay answered no", but the response body can, and
    pytusk preserves that distinction rather than collapsing both into a
    single failure.

    pytusk must NEVER report a paid tip as lost. The relay's freshness
    threshold (operator-configurable, not exposed via ``/v1/tip-config``) is
    unobservable from the client, so claiming loss would assert something
    that cannot be checked. :attr:`RESUMABLE` therefore surfaces the
    resumption tokens -- the Tx1 digest, the blob ID, and the nonce -- so a
    later POST can reuse the tip already paid. The relay implements no
    replay protection: re-POSTing the identical ``tx_id`` and ``nonce`` is
    accepted, and never re-charges.

    Attributes:
        CERTIFIED: The blob is registered, uploaded, and certified.
        RESUMABLE: Tx1 is final and the tip is intact, but the upload did
            not complete. Retrying the POST with the receipt's tokens costs
            nothing further.
        REJECTED: The relay refused definitively (e.g. 400/401/402).
            Resuming will not help; a fixed paid tip cannot satisfy a 402.
        NOT_STARTED: The attempt failed before Tx1 executed. Nothing was
            spent.
    """

    __str__ = str.__str__

    CERTIFIED = "certified"
    RESUMABLE = "resumable"
    REJECTED = "rejected"
    NOT_STARTED = "not_started"


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
        register_tip_tx_digest (str | None): Digest of Tx1 (the bundled
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
    register_tip_tx_digest: str | None
    certify_tx_digest: str | None
    nonce: str | None
    relay_status: int | None
    relay_message: str | None
    attempts: int | None
    transport_error: str | None


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


class RelayUploadError(RuntimeError):
    """Base error for a failed stage of the relay upload pipeline.

    Raised only for contract violations and unrecoverable local faults.
    Operational outcomes -- a refused upload, an unfinished POST -- are
    reported through :class:`RelayBlobReceipt` instead, so that a paid tip
    is never hidden inside a traceback.

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


@dataclasses.dataclass(kw_only=True, frozen=True)
class AuthPackage:
    """The authentication package binding a tip payment to one blob upload.

    Walrus calls this ``HashedAuthPackage``. Its BCS encoding is the FIRST
    input of the register+tip transaction -- ``add_tip`` refuses to compose
    into a transaction that already has inputs, because the relay looks for
    the package at input 0 and nowhere else.

    The package commits to the blob and to a nonce, NOT to the tip amount.
    That is what makes an already-paid tip reusable: the relay implements no
    replay protection, so re-POSTing with the same ``tx_id`` and ``nonce`` is
    accepted and never re-charges. A resumed upload must therefore reuse the
    ORIGINAL package -- building a fresh one abandons the tip already paid.

    Attributes:
        nonce (bytes): 32 random bytes, generated per attempt via
            :func:`secrets.token_bytes`. Sent to the relay as
            :attr:`nonce_base64url`; the transaction commits only to its
            digest, never to the nonce itself.
        blob_digest (bytes): SHA-256 of the unencoded blob bytes.
        nonce_digest (bytes): SHA-256 of :attr:`nonce`.
        unencoded_length (int): Length in bytes of the unencoded blob.
    """

    nonce: bytes
    blob_digest: bytes
    nonce_digest: bytes
    unencoded_length: int

    @property
    def bcs(self) -> bytes:
        """BCS encoding of the package: always exactly 72 bytes.

        BCS writes a fixed-size byte array with no length prefix and a
        ``u64`` little-endian, so the layout is ``blob_digest`` (32 bytes)
        || ``nonce_digest`` (32 bytes) || ``unencoded_length`` (8 bytes).

        Returns:
            bytes: The 72-byte encoding used as transaction input 0.
        """
        return (
            self.blob_digest
            + self.nonce_digest
            + self.unencoded_length.to_bytes(8, "little")
        )

    @property
    def nonce_base64url(self) -> str:
        """The nonce as URL-safe, UNPADDED base64, as the relay expects it.

        Returns:
            str: Base64url encoding of :attr:`nonce` with ``=`` padding
                stripped.
        """
        return base64.urlsafe_b64encode(self.nonce).rstrip(b"=").decode("ascii")


@dataclasses.dataclass(kw_only=True, frozen=True)
class TipResult:
    """The outcome of a standalone tip payment.

    Returned by :func:`~pytusk.core.relay_upload.tip.execute_tip`, which is
    the composable escape hatch: the encapsulated pipeline bundles the tip
    into Tx1 and never calls it. Its reason to exist is resuming -- paying a
    fresh tip for an upload whose earlier attempt is no longer usable -- so
    it carries the tokens a later POST needs, not merely a digest.

    Attributes:
        digest (str): Digest of the tip transaction, sent to the relay as
            its ``tx_id`` query parameter.
        nonce (str): Base64url nonce from the authentication package, sent
            alongside the digest. The relay checks both.
        relay_address (str): Sui address the tip was transferred to.
        tip_amount (int): Tip paid, in MIST.
    """

    digest: str
    nonce: str
    relay_address: str
    tip_amount: int


@dataclasses.dataclass(kw_only=True, frozen=True)
class TipQuote:
    """A relay's tip policy resolved against one specific blob.

    :class:`TipConfig` says how a relay charges; this says what THIS upload
    will actually cost, so a caller can show or approve the figure before
    any transaction is built. It is also what tusky's simulate summary
    reports, which needs no chain state and no registration.

    Attributes:
        address (str | None): Sui address the tip is transferred to, or
            ``None`` when the relay requires no tip.
        amount (int | None): The computed tip in MIST, or ``None`` when the
            relay requires no tip.
        kind (TipKind | None): The formula the amount came from, kept so a
            caller can explain the figure rather than just quote it.
    """

    address: str | None
    amount: int | None
    kind: TipKind | None

    @property
    def requires_payment(self) -> bool:
        """Whether this upload must pay a tip.

        Returns:
            bool: True when a tip must be paid, False for a ``no_tip`` relay.
        """
        return self.kind is not None
