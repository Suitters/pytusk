#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""End-to-end write pipelines.

A pipeline here OWNS THE ORDER and owns the receipt; it does not own the
stages. Encoding lives in :mod:`pytusk.core.encoding`, Tx1 and Tx2 in
:mod:`pytusk.core.ops`, the per-node fan-out and confirmation collection in
:mod:`pytusk.core.native_upload`, the choice of what Tx1 contains in
:mod:`pytusk.core.pipelines.registration`, and the choice between fan-out and
relay in :mod:`pytusk.core.pipelines.delivery`. What is left here is the
sequence, the stage timings, and the mapping from an outcome to the receipt a
caller gets.

This is also why the compose function lives ABOVE ``native_upload`` rather
than inside it. It reaches across two peer packages, and a function that
orchestrates peers cannot sit inside one of them without inverting the
dependency between them.

THE ERROR BOUNDARY IS TX1. A failure BEFORE registration succeeds -- encoding,
package-ID resolution, or Tx1 itself -- has no on-chain state to report and is
RAISED as a typed :class:`~pytusk.core.types.NativeUploadError`. A failure
AFTER it has spent WAL and created a real ``Blob``, so it is RETURNED as a
:class:`~pytusk.core.types.NativeBlobReceipt` with ``certified=False`` and
``failed_stage`` set. Discarding that state into an exception would strand a
blob the caller has already paid for.
"""

import asyncio
import dataclasses
import functools
import time

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.chain.committee import WalrusCommittee
from pytusk.core.encoding import encode_blob
from pytusk.core.encoding.redstuff import EncodedBlob
from pytusk.core.native_upload.certify import certify
from pytusk.core.ops.blob_execute import (
    preflight_payment,
    preflight_sponsor,
    submit_certification,
)
from pytusk.core.ops.system_reads import resolve_package_id
from pytusk.core.pipelines.delivery import (
    DeliveryOutcome,
    NativeDelivery,
    RelayDelivery,
)
from pytusk.core.pipelines.registration import (
    BlobRegistration,
    PlainBlobRegistration,
    TippedBlobRegistration,
)
from pytusk.core.relay_upload.tip import FROM_GAS, build_auth_package, quote_tip
from pytusk.core.relay_upload.upload import DEFAULT_MAX_UPLOAD_ATTEMPTS
from pytusk.core.types import (
    NativeBlobReceipt,
    NativeUploadError,
    Registration,
    RegistrationPendingError,
    RelayBlobReceipt,
    RelayCertifyTransactionError,
    RelayOutcome,
    RelayStageTimings,
    RelayUploadError,
    StageTimings,
    TipComposition,
)


def _failure_receipt(
    *,
    encoded: EncodedBlob,
    registration: Registration,
    error: NativeUploadError | None,
    encode_duration: float,
    register_tx1_duration: float,
    sliver_upload_duration: float | None,
    confirmations_duration: float | None,
    total_duration: float,
) -> NativeBlobReceipt:
    """Build the receipt for a post-registration failure.

    One construction site for both post-spend failure paths (delivery and
    Tx2), so the two cannot drift apart in which facts they carry -- the
    recovery-data gap this refactor exists partly to close.

    Args:
        encoded (EncodedBlob): The encoded blob, for its base64 blob ID.
        registration (Registration): Tx1's result. Its presence is what makes
            this receipt worth returning at all.
        error (NativeUploadError | None): The failure being reported. Only
            :class:`~pytusk.core.types.CertifyTransactionError` carries a
            ``duration``; every other stage's duration is tracked by the
            caller and passed in below.
        encode_duration (float): Duration of the encode stage.
        register_tx1_duration (float): Duration of Tx1.
        sliver_upload_duration (float | None): Duration of the sliver
            fan-out, or ``None`` if it never ran.
        confirmations_duration (float | None): Duration of confirmation
            collection, or ``None`` if it never ran.
        total_duration (float): Total pipeline duration.

    Returns:
        NativeBlobReceipt: An uncertified receipt carrying every recovery
            fact available at the point of failure.
    """
    return NativeBlobReceipt(
        blob_id=encoded.blob_id_base64,
        object_id=registration.object_id,
        certified=False,
        end_epoch=registration.end_epoch,
        failed_stage=error.stage if error is not None else "deliver",
        timings=StageTimings(
            encode=encode_duration,
            register_tx1=register_tx1_duration,
            sliver_upload=sliver_upload_duration,
            confirmations=confirmations_duration,
            # Only CertifyTransactionError sets `duration`, to the Tx2
            # duration submit_certification recorded before re-raising. Every
            # pre-Tx2 failure leaves it None, and the stage durations above
            # are the caller-tracked ones.
            certify_tx2=error.duration if error is not None else None,
            total=total_duration,
        ),
        register_tx_digest=registration.digest,
    )


@dataclasses.dataclass(kw_only=True, frozen=True)
class WritePreamble:
    """Everything a write pipeline must read before it can compose Tx1.

    Attributes:
        committee (WalrusCommittee): The committee for the current epoch.
        encoded (EncodedBlob): The RedStuff-encoded blob.
        encode_duration (float): Seconds spent encoding.
        system_object (str): Object ID of the configured Walrus System object.
        package_id (str): The resolved Walrus package ID.
    """

    committee: WalrusCommittee
    encoded: EncodedBlob
    encode_duration: float
    system_object: str
    package_id: str


async def prepare_write(
    *,
    client: WalrusClient,
    data: bytes,
    error_type: type[NativeUploadError] | type[RelayUploadError],
) -> WritePreamble:
    """Read the committee, encode the blob, and resolve the package ID.

    The three reads EVERY write path needs before Tx1, in the one order they
    can happen in: the encoding needs the committee's shard count, and Tx1
    needs both plus the package ID.

    ``error_type`` exists because the two pipelines report pre-spend failures
    under different names, and NEITHER should be forced to adopt the other's.
    Both :class:`~pytusk.core.types.NativeUploadError` and
    :class:`~pytusk.core.types.RelayUploadError` take the same
    ``(message=, stage=)`` constructor, so passing the class is enough --
    no callback and no third error type invented purely to be translated at
    both call sites.

    Everything here is PRE-SPEND: nothing has been registered and no WAL has
    moved, so a failure has no on-chain state to report and is RAISED rather
    than returned. That is the other half of the rule the post-Tx1 stages
    follow -- see this module's docstring.

    This reads ONLY what every path needs. The staking object, for instance,
    is deliberately absent: only the native path uses it, and a shared
    preamble that reaches for a field one caller needs forces every other
    caller to supply it. That caller reads it itself.

    Args:
        client (WalrusClient): Client used for the committee and package
            reads.
        data (bytes): The blob content to encode.
        error_type (type[NativeUploadError] | type[RelayUploadError]): The
            exception class to raise pre-spend failures as.

    Returns:
        WritePreamble: The committee, the encoded blob, and the identifiers
            Tx1 composition needs.

    Raises:
        NativeUploadError | RelayUploadError: Whichever ``error_type`` names,
            with ``stage`` set to ``"encode"`` or ``"resolve_package_id"``.
    """
    committee = await client.committee()

    encode_start = time.monotonic()
    try:
        encoded = await asyncio.to_thread(
            functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
        )
    except (RuntimeError, ValueError) as exc:
        raise error_type(message=str(exc), stage="encode") from exc
    encode_duration = time.monotonic() - encode_start

    system_object = client.config.network.system_object
    try:
        package_id = await resolve_package_id(
            client=client, system_object=system_object
        )
    except (RuntimeError, ValueError) as exc:
        raise error_type(message=str(exc), stage="resolve_package_id") from exc

    return WritePreamble(
        committee=committee,
        encoded=encoded,
        encode_duration=encode_duration,
        system_object=system_object,
        package_id=package_id,
    )


async def store_blob_native(
    *,
    client: WalrusClient,
    data: bytes,
    epochs: int,
    deletable: bool = False,
    max_confirmation_requests: int = 64,
    wait_millis: int | None = None,
    sender: str | None = None,
    sponsor: str | None = None,
    payment_coin: str | None = None,
    recipient: str | None = None,
) -> NativeBlobReceipt:
    """Run the full native upload pipeline: encode, register, deliver, certify.

    Delivery -- the sliver fan-out and the confirmation quorum -- is performed
    by :class:`~pytusk.core.pipelines.delivery.NativeDelivery`, the same seam
    the relay path uses with a different implementation. This function does
    not call the fan-out or the confirmation collection directly. Tx1 is
    composed and submitted through the matching registration seam,
    :class:`~pytusk.core.pipelines.registration.PlainBlobRegistration`.

    ``client.config.network.system_object`` is the Walrus System object ID
    field defined on ``WalrusNetworkConfig`` in ``pytusk.config.tusk_config``,
    sitting alongside the already-verified ``staking_object`` used by
    :meth:`~pytusk.client.walrus_client.WalrusClient.walrus_epoch` and
    :meth:`~pytusk.client.walrus_client.WalrusClient.committee`.

    See this module's docstring for the error boundary. In short: a failure
    before Tx1 succeeds is raised as a typed
    :class:`~pytusk.core.types.NativeUploadError`; a failure after it is
    returned as an uncertified receipt.
    :class:`~pytusk.core.types.RegistrationPendingError` is the subtle case --
    Tx1 SUCCEEDED on-chain there and only its read-back lagged, so it is
    post-spend and is converted here into a receipt rather than raised.
    ``end_epoch`` is ``None`` on that one path alone, because no
    ``Registration`` was ever obtained and there is nothing real to report.

    ``sender``/``sponsor``/``payment_coin`` are threaded to Tx1. ``recipient``
    is DELIBERATELY NOT passed to Tx1: ``certify_blob`` (Tx2) takes the blob
    as ``&mut Blob`` and must be signed by its owner, so Tx1 always transfers
    the new ``Blob`` to the resolved sender. ``recipient`` is threaded into
    Tx2 instead, which transfers the blob in the SAME PTB immediately after
    certification -- atomic with it, and never handing the object away before
    the sender must sign for it.

    Args:
        client (WalrusClient): Client used for every stage.
        data (bytes): The blob content to store.
        epochs (int): Number of epochs ahead to reserve storage for.
        deletable (bool): Whether the stored blob should be deletable.
        max_confirmation_requests (int): Maximum concurrent confirmation
            requests.
        wait_millis (int | None): Upper bound in milliseconds for each node's
            confirmation long-poll wait.
        sender (str | None): Address to sign Tx1 as. Defaults to the active
            address. Always the owner of the created ``Blob`` after Tx1.
        sponsor (str | None): Address to sponsor Tx1's gas as, or ``None``.
        payment_coin (str | None): Object ID of a ``Coin<WAL>`` to use as Tx1
            payment. When ``None``, one is selected automatically.
        recipient (str | None): Sui address to transfer the certified ``Blob``
            to, as part of Tx2. Used verbatim -- NOT validated. When ``None``,
            the ``Blob`` stays with the resolved ``sender``.

    Returns:
        NativeBlobReceipt: The outcome of the upload attempt, with ``timings``
            populated for every stage that ran -- on both the fully-certified
            and the partial/failed path.

    Raises:
        NativeUploadError: If encoding, package-ID resolution, or Tx1 itself
            fails before registration succeeds.
    """
    pipeline_start = time.monotonic()

    prepared = await prepare_write(
        client=client, data=data, error_type=NativeUploadError
    )
    committee = prepared.committee
    encoded = prepared.encoded
    encode_duration = prepared.encode_duration
    system_object = prepared.system_object
    package_id = prepared.package_id
    # Read here rather than in prepare_write: only this path needs it --
    # certify() re-reads the epoch through it when the committee changes --
    # and a shared preamble that reaches for a field one caller uses would
    # force every other caller's config to carry it.
    staking_object = client.config.network.staking_object

    register_tx1_start = time.monotonic()
    try:
        registration = await PlainBlobRegistration(
            payment_coin=payment_coin,
            sender=sender,
            sponsor=sponsor,
        ).register(
            client=client,
            encoded=encoded,
            epochs=epochs,
            deletable=deletable,
            package_id=package_id,
            system_object=system_object,
        )
    except RegistrationPendingError as exc:
        # Tx1 SUCCEEDED on-chain -- a transient checkpoint finality/read-back
        # lag, not a lost transaction. Converted here, at the pipeline
        # boundary, into a partial receipt rather than propagated: a
        # post-spend recoverable condition must reach the caller as return
        # data. `registration` never bound (the exception interrupted the
        # awaited call before its result did), so `end_epoch` is genuinely
        # unavailable and is left None rather than fabricated. This is the one
        # failure path that cannot use `_failure_receipt`, which requires a
        # `Registration` that does not exist here.
        return NativeBlobReceipt(
            blob_id=encoded.blob_id_base64,
            object_id=exc.object_id,
            certified=False,
            end_epoch=None,
            failed_stage=exc.stage,
            timings=StageTimings(
                encode=encode_duration,
                register_tx1=time.monotonic() - register_tx1_start,
                sliver_upload=None,
                confirmations=None,
                certify_tx2=None,
                total=time.monotonic() - pipeline_start,
            ),
            register_tx_digest=exc.digest,
        )
    except (RuntimeError, ValueError) as exc:
        raise NativeUploadError(message=str(exc), stage="register_tx1") from exc
    register_tx1_duration = time.monotonic() - register_tx1_start

    delivered = await NativeDelivery(
        wait_millis=wait_millis,
        max_confirmation_requests=max_confirmation_requests,
    ).deliver(
        client=client,
        committee=committee,
        encoded=encoded,
        data=data,
        registration=registration,
    )

    if delivered.outcome is not DeliveryOutcome.DELIVERED:
        # NativeDelivery REPORTS a post-spend failure rather than raising it
        # (see its module docstring), which is what lets the stage durations
        # recorded up to the failure reach this receipt.
        return _failure_receipt(
            encoded=encoded,
            registration=registration,
            error=delivered.error,
            encode_duration=encode_duration,
            register_tx1_duration=register_tx1_duration,
            sliver_upload_duration=delivered.sliver_upload_duration,
            confirmations_duration=delivered.confirmations_duration,
            total_duration=time.monotonic() - pipeline_start,
        )

    try:
        receipt = await certify(
            client=client,
            committee=committee,
            blob_id=encoded.blob_id,
            registration=registration,
            certificate=delivered.require_certificate(),
            package_id=package_id,
            system_object=system_object,
            staking_object=staking_object,
            recipient=recipient,
            stage_timings=StageTimings(
                encode=encode_duration,
                register_tx1=register_tx1_duration,
                sliver_upload=delivered.sliver_upload_duration,
                confirmations=delivered.confirmations_duration,
                certify_tx2=None,
                total=None,
            ),
        )
    except NativeUploadError as exc:
        return _failure_receipt(
            encoded=encoded,
            registration=registration,
            error=exc,
            encode_duration=encode_duration,
            register_tx1_duration=register_tx1_duration,
            sliver_upload_duration=delivered.sliver_upload_duration,
            confirmations_duration=delivered.confirmations_duration,
            total_duration=time.monotonic() - pipeline_start,
        )

    return dataclasses.replace(
        receipt,
        timings=dataclasses.replace(
            receipt.timings, total=time.monotonic() - pipeline_start
        ),
    )


async def store_blob_relay(
    *,
    client: WalrusClient,
    data: bytes,
    epochs: int,
    deletable: bool = False,
    relay_name: str | None = None,
    sender: str | None = None,
    sponsor: str | None = None,
    recipient: str | None = None,
    tip_source: str = FROM_GAS,
    wal_payment_coin: str | None = None,
    max_upload_attempts: int = DEFAULT_MAX_UPLOAD_ATTEMPTS,
) -> RelayBlobReceipt:
    """Store a blob through a Walrus upload relay, end to end.

    The config-driven, execute-only entry point: hand it bytes and an epoch
    count and it registers, tips, uploads, and certifies. Callers needing
    URL-level or transaction-level control use the composable functions in
    :mod:`pytusk.core.relay_upload` directly.

    The stage order is forced by the relay protocol and cannot be rearranged::

        encode -> committee -> tip quote -> [tip + register, ONE PTB] ->
        POST (with retry) -> parse certificate -> certify

    The blob MUST be registered before the POST (the relay answers 401
    ``BlobIdNotRegistered`` otherwise), and the tip MUST be settled before the
    POST because the relay independently re-verifies the payment on chain.
    Certification can never share a PTB with the tip: its input is the
    certificate returned by the POST, which does not exist until the tip
    transaction has landed.

    The POST and the certificate parse are performed by
    :class:`~pytusk.core.pipelines.delivery.RelayDelivery` -- the same seam
    :func:`store_blob_native` uses with a different implementation. Tx1 goes
    through the registration seam:
    :class:`~pytusk.core.pipelines.registration.TippedBlobRegistration` when
    the relay's quote requires a tip, and the same
    :class:`~pytusk.core.pipelines.registration.PlainBlobRegistration` the
    native path uses when it does not.

    Failures are reported, not raised. Once the tip is paid and the blob is
    registered, every later failure returns a ``RelayBlobReceipt`` whose
    ``outcome`` is ``RESUMABLE`` and which carries the resumption tokens --
    the transaction digest, the blob id, and the base64url nonce. Raising
    would discard exactly the values a caller needs to recover. This includes
    :class:`~pytusk.core.types.errors.RegistrationPendingError`: Tx1 there
    SUCCEEDED on-chain (a transient checkpoint finality/read-back lag, not a
    lost transaction), so it is caught here and converted into a ``RESUMABLE``
    receipt rather than propagated. Exceptions are reserved for contract
    violations detected before anything is spent.

    Args:
        client: Walrus client providing config, transport, and transactions.
        data: Raw bytes to store.
        epochs: Number of epochs of storage to reserve.
        deletable: Whether the created ``Blob`` is deletable.
        relay_name: Relay to use. ``None`` resolves the network's
            ``active_relay``.
        sender: Address to send from. ``None`` uses the active address.
        sponsor: Address to sponsor gas. Must be signable in the active
            ``PysuiConfiguration`` -- external-sponsor flows belong in the
            composable layer, because the certify transaction cannot be
            pre-built for outside signing (it needs both the blob object id
            from the register transaction and the certificate from the POST).
        recipient: Address the certified ``Blob`` is transferred to,
            atomically with certification. ``None`` leaves it with sender.
        tip_source: ``"from_gas"`` to split the tip from the gas coin --
            whoever funds the transaction pays -- or a coin object id to split
            from. A coin id is verified against the resolved sender and
            sponsor before anything is built.
        wal_payment_coin: WAL coin funding storage. ``None`` auto-selects.
        max_upload_attempts: POST retry budget.

    Returns:
        A ``RelayBlobReceipt``. Inspect ``outcome`` -- reading ``blob_id``
        alone cannot distinguish success from failure.

    Raises:
        RelayUploadError: A contract violation caught before any spend.
        ValueError: No relay resolved for the active network.
    """
    pipeline_start = time.monotonic()

    resolved_sender = sender or client.pysui_client.config.active_address

    # FAIL FAST: sponsor signability is checked FIRST, before any encode or
    # network work -- this pipeline's certify transaction (Tx2) is
    # auto-signed internally and cannot be pre-built for external signing,
    # so an unsignable sponsor must be caught before anything is spent. See
    # preflight_sponsor's docstring.
    await preflight_sponsor(client=client, sponsor=sponsor)

    # `max_upload_attempts` is otherwise only validated inside
    # relay_upload.upload, which is not reached until AFTER the tip and Tx1
    # have landed. That is post-spend; the caller-side contract violation
    # must be caught here, before anything is spent.
    if max_upload_attempts < 1:
        raise RelayUploadError(
            message=(
                "max_upload_attempts must be at least 1, got "
                f"{max_upload_attempts}"
            ),
            stage="preflight",
        )

    relay_url = client.config.relay_url_for(
        network_name=client.config.active_network, relay_name=relay_name
    )

    # Package-ID resolution now happens inside this call, which puts it
    # BEFORE the tip quote rather than after. It depends on neither the quote
    # nor the auth package, so the relay protocol's fixed stage order is
    # unaffected -- see this package's docstring for what that order actually
    # constrains.
    prepared = await prepare_write(
        client=client, data=data, error_type=RelayUploadError
    )
    committee = prepared.committee
    encoded = prepared.encoded
    encode_duration = prepared.encode_duration
    system_object = prepared.system_object
    package_id = prepared.package_id

    tip_config_start = time.monotonic()
    quote = await quote_tip(
        client=client,
        relay_url=relay_url,
        unencoded_length=len(data),
        n_shards=committee.n_shards,
    )
    tip_config_duration = time.monotonic() - tip_config_start

    auth_package = build_auth_package(data=data) if quote.requires_payment else None

    # Shared EXECUTE-layer preflight covering what used to be hand-copied
    # at this point: the tip-coin usability check (skipped by passing
    # tip_source=None whenever no tip is required) and WAL payment coin
    # resolution. Sponsor signability was already checked above, before any
    # of this pipeline's encode/network work.
    resolved_wal_coin = await preflight_payment(
        client=client,
        sender=resolved_sender,
        sponsor=sponsor,
        tip_source=tip_source if quote.requires_payment else None,
        tip_minimum_balance=quote.amount or 0,
        wal_payment_coin=wal_payment_coin,
    )

    register_start = time.monotonic()
    tip_composition = (
        TipComposition(
            relay_address=quote.address or "",
            tip_amount=quote.amount or 0,
            auth_package=auth_package,
            payment_coin=tip_source,
        )
        if auth_package is not None
        else None
    )
    # THE VARIANT SELECTION POINT. With a tip to bundle, Tx1 must carry it in
    # the same PTB -- the relay re-verifies the payment on chain and reads the
    # authentication package at PTB input 0. With no tip required, a relay's
    # Tx1 IS a plain registration, composing the identical PTB the native path
    # does, so it reuses that implementation rather than a third one that
    # would be free to drift from it.
    registration_strategy: BlobRegistration = (
        TippedBlobRegistration(
            tip=tip_composition,
            wal_payment_coin=resolved_wal_coin,
            sender=resolved_sender,
            sponsor=sponsor,
        )
        if tip_composition is not None
        else PlainBlobRegistration(
            payment_coin=resolved_wal_coin,
            sender=resolved_sender,
            sponsor=sponsor,
        )
    )
    nonce = auth_package.nonce_base64url if auth_package is not None else None
    tip_paid = auth_package is not None
    try:
        registration = await registration_strategy.register(
            client=client,
            encoded=encoded,
            epochs=epochs,
            deletable=deletable,
            package_id=package_id,
            system_object=system_object,
        )
    except RegistrationPendingError as exc:
        # Tx1 SUCCEEDED on-chain (tip included, if any) -- this is a
        # transient checkpoint finality/read-back lag, not a lost
        # transaction. Converted HERE, at the pipeline boundary, into a
        # RESUMABLE RelayBlobReceipt rather than raised: a post-spend
        # recoverable condition must reach the caller as return data.
        # `registration` was never assigned (the exception interrupted the
        # awaited call before its result bound), so `end_epoch` is
        # genuinely unavailable -- left `None` rather than fabricated. The
        # relay POST and certify stages never ran, so their fields are
        # left at their "did not run" values.
        return RelayBlobReceipt(
            outcome=RelayOutcome.RESUMABLE,
            blob_id=encoded.blob_id_base64,
            object_id=exc.object_id,
            certified=False,
            end_epoch=None,
            failed_stage=exc.stage,
            timings=RelayStageTimings(
                encode=encode_duration,
                tip_config=tip_config_duration,
                register_tip_tx=time.monotonic() - register_start,
                relay_upload=None,
                certify_tx=None,
                total=time.monotonic() - pipeline_start,
            ),
            relay_url=relay_url,
            tip_paid=tip_paid,
            tip_amount=quote.amount,
            register_tx_digest=exc.digest,
            certify_tx_digest=None,
            nonce=nonce,
            relay_status=None,
            relay_message=None,
            attempts=None,
            transport_error=None,
        )
    except (RuntimeError, ValueError) as exc:
        raise RelayUploadError(message=str(exc), stage="register_tx1") from exc
    register_duration = time.monotonic() - register_start

    delivered = await RelayDelivery(
        relay_url=relay_url,
        nonce=nonce,
        tip_paid=tip_paid,
        max_attempts=max_upload_attempts,
    ).deliver(
        client=client,
        committee=committee,
        encoded=encoded,
        data=data,
        registration=registration,
    )
    upload = delivered.upload

    if delivered.outcome is not DeliveryOutcome.DELIVERED:
        return RelayBlobReceipt(
            outcome=(
                RelayOutcome.REJECTED
                if delivered.outcome is DeliveryOutcome.REFUSED
                else RelayOutcome.RESUMABLE
            ),
            blob_id=encoded.blob_id_base64,
            object_id=registration.object_id,
            certified=False,
            end_epoch=registration.end_epoch,
            failed_stage=delivered.failed_stage,
            timings=RelayStageTimings(
                encode=encode_duration,
                tip_config=tip_config_duration,
                register_tip_tx=register_duration,
                relay_upload=upload.duration,
                # Stays None even when the relay ANSWERED and only its
                # certificate failed to parse: Tx2 was never submitted, and
                # reporting a duration for a transaction that never ran is
                # what the previous hand-written version did wrong here.
                certify_tx=None,
                total=time.monotonic() - pipeline_start,
            ),
            relay_url=relay_url,
            tip_paid=tip_paid,
            tip_amount=quote.amount,
            register_tx_digest=registration.digest,
            certify_tx_digest=None,
            nonce=nonce,
            relay_status=upload.relay_status,
            relay_message=(
                str(delivered.error)
                if delivered.error is not None
                else upload.relay_message
            ),
            attempts=upload.attempts,
            transport_error=upload.transport_error,
        )

    certify_start = time.monotonic()
    try:
        # The shared Tx2 stage -- the same one the native path reaches
        # through certify(). Using it gains relay the local
        # verify_certificate check it previously lacked: a certificate that
        # does not verify against the committee's public keys now fails
        # here, for free, instead of aborting on-chain after gas is spent.
        certification = await submit_certification(
            client=client,
            committee=committee,
            registration=registration,
            certificate=delivered.require_certificate(),
            package_id=package_id,
            system_object=system_object,
            error_type=RelayCertifyTransactionError,
            sender=sender,
            sponsor=sponsor,
            recipient=recipient,
        )
    except RuntimeError as exc:
        return RelayBlobReceipt(
            outcome=RelayOutcome.RESUMABLE,
            blob_id=encoded.blob_id_base64,
            object_id=registration.object_id,
            certified=False,
            end_epoch=registration.end_epoch,
            failed_stage="certify",
            timings=RelayStageTimings(
                encode=encode_duration,
                tip_config=tip_config_duration,
                register_tip_tx=register_duration,
                relay_upload=upload.duration,
                certify_tx=time.monotonic() - certify_start,
                total=time.monotonic() - pipeline_start,
            ),
            relay_url=relay_url,
            tip_paid=tip_paid,
            tip_amount=quote.amount,
            register_tx_digest=registration.digest,
            certify_tx_digest=None,
            nonce=nonce,
            relay_status=upload.relay_status,
            relay_message=str(exc),
            attempts=upload.attempts,
            transport_error=None,
        )
    # Taken from the stage itself rather than re-measured here: two callers
    # timing the same work would otherwise be free to measure different
    # spans of it.
    certify_duration = certification.duration

    return RelayBlobReceipt(
        outcome=RelayOutcome.CERTIFIED,
        blob_id=encoded.blob_id_base64,
        object_id=certification.result.object_id,
        certified=True,
        end_epoch=registration.end_epoch,
        failed_stage=None,
        timings=RelayStageTimings(
            encode=encode_duration,
            tip_config=tip_config_duration,
            register_tip_tx=register_duration,
            relay_upload=upload.duration,
            certify_tx=certify_duration,
            total=time.monotonic() - pipeline_start,
        ),
        relay_url=relay_url,
        tip_paid=tip_paid,
        tip_amount=quote.amount,
        register_tx_digest=registration.digest,
        certify_tx_digest=certification.result.digest,
        nonce=nonce,
        relay_status=upload.relay_status,
        relay_message=upload.relay_message,
        attempts=upload.attempts,
        transport_error=None,
    )
