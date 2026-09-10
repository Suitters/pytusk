#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""The delivery seam: how an already-registered blob reaches storage nodes.

Native upload fans encoded slivers out to every storage node itself and then
polls those nodes for a confirmation quorum; relay upload POSTs the RAW,
UNENCODED bytes to a single relay that performs the fan-out server-side and
returns an already-quorum-backed certificate. Those are the SAME step in the
write pipeline -- get the blob to the nodes and come back with a certificate
ready for ``certify_blob`` -- reached two different ways, and
:class:`BlobDelivery` is the one seam that expresses the difference.

The seam is deliberately WIDER than "upload". If it stopped at handing the
bytes over, a shared pipeline would still have to branch on whether a
separate confirmation-collection stage must run, because native needs one and
relay does not. Obtaining the certificate is therefore part of this contract,
not a stage layered on top of it.

The Walrus PUBLISHER path is NOT a delivery strategy and is deliberately
absent here. A publisher performs registration, encoding, fan-out,
confirmation AND certification server-side from a single PUT (see
:class:`~pytusk.commands.write_commands.WriteBlob`); it has no Tx1 and no Tx2
of its own. Modelling it as a strategy inside a register -> deliver ->
certify pipeline would mean an implementation whose registration and
certification steps are both no-ops -- fitting a concept to a module that
does not hold it, rather than letting it keep the shape it actually has.

FAILURE IS REPORTED, NOT RAISED on this seam. Delivery runs strictly AFTER
Tx1 has committed, so every failure here is post-spend: storage is already
paid for and the blob is already registered on chain. Raising would discard
the facts a caller needs to resume. :meth:`BlobDelivery.deliver` therefore
returns a :class:`DeliveryResult` carrying a :class:`DeliveryOutcome`, and
callers MUST branch on that outcome rather than on whether
:attr:`DeliveryResult.certificate` happens to be set.

This holds on BOTH paths. :class:`NativeDelivery` catches
:class:`~pytusk.core.types.SliverUploadError` and
:class:`~pytusk.core.types.ConfirmationCollectionError` and reports them on
:attr:`NativeDeliveryResult.error` rather than raising. The catch lives here
rather than in the caller for one reason: only this function knows how far
each of its two stages got, and
:class:`~pytusk.core.types.StageTimings` guarantees that a stage's duration
is recorded even when that stage FAILS -- a fan-out that ran for forty
minutes before dying still reports those forty minutes. Raising past this
point would discard exactly that.
"""

import dataclasses
import enum
import time
import typing

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.certification import Certificate
from pytusk.core.chain.committee import WalrusCommittee
from pytusk.core.encoding import object_id_to_raw_bytes
from pytusk.core.encoding.redstuff import EncodedBlob
from pytusk.core.native_upload.confirm import collect_confirmations
from pytusk.core.native_upload.fanout import upload_slivers
from pytusk.core.relay_upload.relay_certify import parse_relay_certificate
from pytusk.core.relay_upload.upload import (
    DEFAULT_MAX_UPLOAD_ATTEMPTS,
    upload_to_relay,
)
from pytusk.core.types import (
    NativeUploadError,
    Registration,
    RelayCertificateParseError,
    RelayUploadError,
    RelayUploadOutcome,
    RelayUploadResult,
)


class DeliveryOutcome(enum.Enum):
    """Terminal state of the delivery stage.

    Deliberately SMALLER than the relay's own
    :class:`~pytusk.core.types.RelayUploadOutcome`: this vocabulary has to
    mean the same thing on a path that fans out to a hundred storage nodes
    and on a path that makes one HTTP POST, so it records only what a caller
    must branch on. Relay-specific detail survives on
    :attr:`RelayDeliveryResult.upload`, not by widening this enum.

    Attributes:
        DELIVERED: The blob reached storage and a certificate was obtained.
        RESUMABLE: No certificate, but the attempt may be retried as-is. The
            spend already made is not lost.
        REFUSED: The delivery target deterministically rejected the blob.
            Retrying the identical request will be refused again.
    """

    DELIVERED = "delivered"
    RESUMABLE = "resumable"
    REFUSED = "refused"


@dataclasses.dataclass(kw_only=True, frozen=True)
class DeliveryResult:
    """The honest intersection of what every delivery strategy reports.

    Only facts that mean the same thing on every path live here. Path-specific
    detail belongs on a subclass -- see :class:`NativeDeliveryResult` and
    :class:`RelayDeliveryResult` -- so that a caller on one path is never
    handed a row of Optional fields that only carry meaning on the other.
    :meth:`BlobDelivery.deliver` is annotated as returning this base type, and
    a Protocol's return type is checked COVARIANTLY, so returning a subclass
    satisfies the contract without widening it for anyone.

    Attributes:
        outcome (DeliveryOutcome): Terminal state of the stage. Branch on
            this, never on whether ``certificate`` is set.
        certificate (Certificate | None): The quorum-backed certificate,
            present if and only if ``outcome`` is
            :attr:`DeliveryOutcome.DELIVERED`.
        duration (float): Wall-clock seconds spent in ``deliver()``, measured
            with :func:`time.monotonic`.
        failed_stage (str | None): Name of the stage that failed, or ``None``
            on success. NAMED BY THE STRATEGY, because the strategy is the
            only thing that knows how far it got -- a caller cannot infer
            "the POST was refused" from "there is no certificate", and the
            two are different facts to a user trying to recover.
    """

    outcome: DeliveryOutcome
    certificate: Certificate | None
    duration: float
    failed_stage: str | None = None

    def require_certificate(self) -> Certificate:
        """Return the certificate, which is present whenever delivery succeeded.

        The outcome enum, not the presence of :attr:`certificate`, is the
        source of truth for whether delivery succeeded -- see this module's
        note that callers must branch on the outcome. That branch alone does
        not narrow :attr:`certificate` for a type checker, because the field
        is declared independently of the outcome. This accessor closes that
        gap without a cast: callers that have already established a
        ``DELIVERED`` outcome call it and receive a non-optional
        :class:`~pytusk.core.certification.Certificate`.

        Returns:
            Certificate: The delivery certificate.

        Raises:
            RuntimeError: If called on a result whose outcome is not
                ``DELIVERED``. This is a programming error in the caller,
                not a delivery failure -- a failed delivery is reported
                through the outcome, never through this method.
        """
        if self.certificate is None:
            raise RuntimeError(
                "require_certificate() called on a DeliveryResult with "
                f"outcome {self.outcome}; the certificate is only present "
                "when the outcome is DELIVERED."
            )
        return self.certificate


@dataclasses.dataclass(kw_only=True, frozen=True)
class NativeDeliveryResult(DeliveryResult):
    """A native delivery's result, with its two stages timed separately.

    Native delivery is two distinct network stages, and the receipt the write
    pipeline ultimately builds reports them as separate timings. A single
    total would discard that, so the split is carried here rather than on
    :class:`DeliveryResult`, where it would be meaningless to a relay caller.

    Attributes:
        sliver_upload_duration (float): Seconds spent in the sliver fan-out.
        confirmations_duration (float | None): Seconds spent collecting the
            confirmation quorum. ``None`` when the fan-out failed and this
            stage was never entered -- never ``0.0``, which would be
            indistinguishable from "ran and took no time".
        error (NativeUploadError | None): The failure being reported, or
            ``None`` on success. Carries the name of the failing stage and,
            for a stage that tracked one, its own duration.
    """

    sliver_upload_duration: float
    confirmations_duration: float | None
    error: NativeUploadError | None = None


@dataclasses.dataclass(kw_only=True, frozen=True)
class RelayDeliveryResult(DeliveryResult):
    """A relay delivery's result, carrying the POST stage's own report.

    The relay's recovery facts -- HTTP status, relay message, attempt count,
    transport error, and the resumption tokens -- are NOT re-declared field by
    field here. The POST stage already models them as
    :class:`~pytusk.core.types.RelayUploadResult`, so that value is carried
    whole. Re-listing its fields would duplicate a contract that already
    exists and would silently rot the moment the POST stage gains one.

    Attributes:
        upload (RelayUploadResult): The POST stage's full report, including
            every token needed to resume. Present on success and failure
            alike.
        error (RelayUploadError | None): The failure being reported when the
            relay ANSWERED but its certificate could not be parsed;
            ``None`` otherwise. A refused or unanswered POST is described by
            ``upload`` instead and leaves this ``None`` -- the relay
            answering with something unusable and the relay not answering at
            all are separate outcomes and must not collapse into one.
    """

    upload: RelayUploadResult
    error: RelayUploadError | None = None


@typing.runtime_checkable
class BlobDelivery(typing.Protocol):
    """How a registered blob reaches storage and yields a certificate.

    The write path's single real branch point. Everything before it (encode,
    Tx1) and everything after it (Tx2) is common to every strategy; this is
    the one step native and relay genuinely do differently.

    Path-specific configuration belongs on the IMPLEMENTATION, supplied when
    it is constructed -- a relay URL and nonce mean nothing to native, and a
    confirmation-request cap means nothing to relay. :meth:`deliver` therefore
    takes only what every strategy genuinely needs per call.
    """

    async def deliver(
        self,
        *,
        client: WalrusClient,
        committee: WalrusCommittee,
        encoded: EncodedBlob,
        data: bytes,
        registration: Registration,
    ) -> DeliveryResult:
        """Deliver a registered blob and obtain its certificate.

        Both representations of the blob are passed because the strategies
        need different ones: native transmits ``encoded``'s slivers, while a
        relay is handed the raw ``data`` and encodes server-side. Neither can
        be derived from the other cheaply at this point, and both are already
        in the caller's hand.

        Args:
            client (WalrusClient): Client used to issue requests.
            committee (WalrusCommittee): Committee for the current epoch. The
                certificate is only valid against this epoch's signer
                ordering.
            encoded (EncodedBlob): The RedStuff-encoded blob.
            data (bytes): The raw, UNENCODED blob bytes.
            registration (Registration): Tx1's result.

        Returns:
            DeliveryResult: The certificate, or the facts needed to resume.
                Implementations may return a subclass carrying path-specific
                detail.
        """
        ...


@dataclasses.dataclass(kw_only=True, frozen=True)
class NativeDelivery:
    """Delivery by fanning slivers out to the committee directly.

    Runs the two native stages in their FIXED order: every node's metadata
    and slivers are PUT first, and only then is that same committee polled
    for a confirmation quorum. The order is not an implementation detail --
    a storage node will not confirm a blob whose slivers it has not received.

    Attributes:
        wait_millis (int | None): Upper bound in milliseconds for each node's
            confirmation long-poll.
        max_confirmation_requests (int): Maximum concurrent confirmation
            requests.
    """

    wait_millis: int | None = None
    max_confirmation_requests: int = 64

    async def deliver(
        self,
        *,
        client: WalrusClient,
        committee: WalrusCommittee,
        encoded: EncodedBlob,
        data: bytes,
        registration: Registration,
    ) -> NativeDeliveryResult:
        """Fan slivers out to every node, then collect a confirmation quorum.

        Args:
            client (WalrusClient): Client used to issue requests.
            committee (WalrusCommittee): Committee for the current epoch.
            encoded (EncodedBlob): The RedStuff-encoded blob whose slivers are
                fanned out.
            data (bytes): Unused. Native transmits ``encoded``'s slivers, never
                the raw bytes; the parameter exists because the seam is shared
                with a strategy that does need them.
            registration (Registration): Tx1's result, identifying the blob and
                whether it is deletable.

        Returns:
            NativeDeliveryResult: :attr:`DeliveryOutcome.DELIVERED` with the
                certificate on success. On a quorum failure in either stage,
                :attr:`DeliveryOutcome.RESUMABLE` with no certificate, the
                failure on ``error``, and every stage duration recorded up to
                the point it failed.
        """
        del data  # See the Args entry: native never transmits raw bytes.
        started = time.monotonic()

        sliver_upload_start = time.monotonic()
        try:
            await upload_slivers(
                client=client,
                committee=committee,
                encoded=encoded,
            )
        except NativeUploadError as exc:
            return NativeDeliveryResult(
                outcome=DeliveryOutcome.RESUMABLE,
                certificate=None,
                duration=time.monotonic() - started,
                failed_stage=exc.stage,
                sliver_upload_duration=time.monotonic() - sliver_upload_start,
                confirmations_duration=None,
                error=exc,
            )
        sliver_upload_duration = time.monotonic() - sliver_upload_start

        confirmations_start = time.monotonic()
        try:
            certificate = await collect_confirmations(
                client=client,
                committee=committee,
                blob_id=encoded.blob_id,
                registration=registration,
                wait_millis=self.wait_millis,
                max_confirmation_requests=self.max_confirmation_requests,
            )
        except NativeUploadError as exc:
            return NativeDeliveryResult(
                outcome=DeliveryOutcome.RESUMABLE,
                certificate=None,
                duration=time.monotonic() - started,
                failed_stage=exc.stage,
                sliver_upload_duration=sliver_upload_duration,
                confirmations_duration=time.monotonic() - confirmations_start,
                error=exc,
            )
        confirmations_duration = time.monotonic() - confirmations_start

        return NativeDeliveryResult(
            outcome=DeliveryOutcome.DELIVERED,
            certificate=certificate,
            duration=time.monotonic() - started,
            sliver_upload_duration=sliver_upload_duration,
            confirmations_duration=confirmations_duration,
        )


@dataclasses.dataclass(kw_only=True, frozen=True)
class RelayDelivery:
    """Delivery by POSTing the raw blob to an upload relay.

    The relay performs encoding, fan-out and confirmation collection on the
    client's behalf and returns an already-quorum-backed certificate, which is
    what makes mainnet writes practical. Its JSON certificate is parsed here
    rather than by the caller, so that this seam always yields the same
    :class:`~pytusk.core.certification.Certificate` type the native path
    produces.

    ``deletable_blob_object`` is derived from
    :attr:`~pytusk.core.types.Registration.deletable` -- the value read back
    from chain after Tx1 -- rather than from a separately supplied flag, so
    the request can never disagree with what was actually registered.

    Attributes:
        relay_url (str): Base URL of the relay to POST to.
        nonce (str | None): Base64url nonce from the authentication package.
            ``None`` only against a ``no_tip`` relay.
        tip_paid (bool): Whether Tx1 carried a tip. When ``False``, no
            ``tx_id`` is sent, which is valid only against a ``no_tip`` relay.
        max_attempts (int): POST attempt budget, at least 1.
        timeout (float | None): Per-attempt request timeout, in seconds.
            ``None`` uses the client's configured default, NOT "no
            timeout". Every retry re-sends the body from the beginning,
            so a large blob on a slow link needs this raised.
    """

    relay_url: str
    nonce: str | None = None
    tip_paid: bool = False
    max_attempts: int = DEFAULT_MAX_UPLOAD_ATTEMPTS
    timeout: float | None = None

    async def deliver(
        self,
        *,
        client: WalrusClient,
        committee: WalrusCommittee,
        encoded: EncodedBlob,
        data: bytes,
        registration: Registration,
    ) -> RelayDeliveryResult:
        """POST the raw blob to the relay and parse the certificate it returns.

        Args:
            client (WalrusClient): Client used to issue the POST.
            committee (WalrusCommittee): Committee for the epoch the relay
                certified against; required to derive the certificate's signer
                bitmap and weight.
            encoded (EncodedBlob): Used only for its blob ID. The relay encodes
                the blob itself, so no sliver from this is transmitted.
            data (bytes): The raw, UNENCODED blob bytes sent to the relay.
            registration (Registration): Tx1's result, supplying the tip
                transaction digest and the deletable object ID.

        Returns:
            RelayDeliveryResult: The parsed certificate on success, otherwise
                the outcome and the tokens needed to resume. A refused upload
                reports :attr:`DeliveryOutcome.REFUSED`; an unanswered one,
                or one whose certificate cannot be parsed, reports
                :attr:`DeliveryOutcome.RESUMABLE`.
        """
        started = time.monotonic()

        upload = await upload_to_relay(
            client=client,
            relay_url=self.relay_url,
            blob_id=encoded.blob_id_base64,
            data=data,
            register_tip_tx_digest=registration.digest if self.tip_paid else None,
            nonce=self.nonce,
            timeout=self.timeout,
            deletable_blob_object=(
                registration.object_id if registration.deletable else None
            ),
            max_attempts=self.max_attempts,
        )

        if upload.outcome is not RelayUploadOutcome.UPLOADED or upload.certificate is None:
            return RelayDeliveryResult(
                outcome=(
                    DeliveryOutcome.REFUSED
                    if upload.outcome is RelayUploadOutcome.REFUSED
                    else DeliveryOutcome.RESUMABLE
                ),
                certificate=None,
                duration=time.monotonic() - started,
                failed_stage="relay_upload",
                upload=upload,
            )

        try:
            certificate = parse_relay_certificate(
                payload=upload.certificate,
                committee=committee,
                blob_id=encoded.blob_id,
                object_id=(
                    object_id_to_raw_bytes(object_id=registration.object_id)
                    if registration.deletable
                    else None
                ),
            )
        except RelayCertificateParseError as exc:
            # The POST succeeded, so the tip is spent and the blob is with
            # the relay: this is post-spend and is REPORTED, exactly as a
            # refused upload is. Raising here would discard the resumption
            # tokens on `upload`. The stage is named "certify" rather than
            # "relay_upload" because the upload itself did not fail -- the
            # relay answered, with something this client cannot use.
            return RelayDeliveryResult(
                outcome=DeliveryOutcome.RESUMABLE,
                certificate=None,
                duration=time.monotonic() - started,
                failed_stage="certify",
                upload=upload,
                error=exc,
            )

        return RelayDeliveryResult(
            outcome=DeliveryOutcome.DELIVERED,
            certificate=certificate,
            duration=time.monotonic() - started,
            upload=upload,
        )
