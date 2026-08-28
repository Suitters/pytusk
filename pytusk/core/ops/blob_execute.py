#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""EXECUTE layer for blob registration and certification transactions.

Contains :func:`preflight_sponsor` (sponsor-signability validation) and
:func:`preflight_payment` (tip-coin usability validation and WAL payment
coin resolution) -- the guards that must run before Tx1 is composed, shared
by every caller so a guard added for one caller cannot silently go missing
for another, which is exactly the drift that had crept in between
:mod:`pytusk.core.pipelines.write` and the ``tusky`` relay CLI's
``simulate`` mode before these functions existed. They are split into two
because they cannot both run at the earliest possible point: sponsor
signability needs only config and so can run before any encode or network
work, while the tip-coin check needs a quote's ``tip_source``, which is
only available after the tip has been quoted. A caller that needs the
fail-fast sponsor guard restored to the front of its pipeline calls
:func:`preflight_sponsor` first and :func:`preflight_payment` once the tip
quote is in hand -- see :mod:`pytusk.core.pipelines.write` for the
concrete ordering.

Also contains :func:`execute_registration_txn` (the shared sign/submit/read-back tail for
Tx1, reused by both :func:`execute_reserve_and_register` below and by
:mod:`pytusk.core.pipelines.write`'s bundled Tx1+tip PTB shape),
:func:`execute_reserve_and_register` (thin wrapper: opens a transaction,
delegates composition to
:func:`~pytusk.core.ops.blob_compose.add_registration_sequence`, then signs
and submits via :func:`execute_registration_txn`), and
:func:`execute_certify` (thin wrapper around
:func:`~pytusk.core.ops.blob_compose.add_certify`: opens a transaction,
composes, then builds, signs, and submits Tx2 itself -- ``certify_blob``
has no read-back step, so it does not share :func:`execute_registration_txn`).

The Blob-field extraction these lean on is NOT duplicated here. Tx1's
read-back calls
:func:`~pytusk.core.chain.blob_fields.blob_deletable_and_end_epoch` -- the
single implementation, which lives in ``chain`` because it takes no client
and only parses an already-fetched object. A private copy lived in this
module until Plan #28 step 11, justified by a one-way dependency
(``tusky`` depends on ``client``, not the reverse) that step 10 dissolved
by moving the original out of the CLI and into ``core``.

Everything Sui-level here is built through pysui, following the exact PTB
patterns already proven in ``pytusk.tusky.tusky_cmds_lifecycle``
(``extend_blob_expiration``, ``delete_blob``, ``burn_blob``) and
``pytusk.tusky.tusky_cmds_exchange`` (``exchange_for_wal``): a transaction is
opened via :meth:`~pytusk.client.walrus_client.WalrusClient.transaction`,
a command's result is threaded directly into a later command's ``arguments``
list (never fetched back as a standalone object), and the built PTB is
signed and submitted with ``ExecuteTransaction(**txdict)``. Status is read
from ``result_data.effects.status`` -- NOT
``result_data.transaction.effects.status`` -- matching the confirmed pysui
result shape used throughout ``tests/integration_tests/conftest.py``.

These are THIN CONVENIENCE WRAPPERS -- see
:mod:`pytusk.core.ops.blob_compose`'s docstring for the "caller owns the
transaction lifecycle" split. They exist for a caller who wants the old
one-call behaviour and does not need to compose Tx1/Tx2 by hand.
"""

import dataclasses
import time

from pysui import ExecuteTransaction, GetObject
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.certification import Certificate, verify_certificate
from pytusk.core.chain import (
    WalrusCommittee,
    blob_deletable_and_end_epoch,
    find_created_object_id,
    require_success,
)
from pytusk.core.encoding import EncodedBlob
from pytusk.core.ops.blob_compose import add_certify, add_registration_sequence
from pytusk.core.ops.coins import assert_coin_usable, select_wal_payment_coin
from pytusk.core.ops.system_reads import (
    DEFAULT_FINALITY_MAX_ATTEMPTS,
    DEFAULT_FINALITY_MAX_DELAY,
    wait_for_finality,
)
from pytusk.core.types.errors import (
    CertifyTransactionError,
    RegistrationPendingError,
    RelayUploadError,
)
from pytusk.core.types.receipts import CertifyResult, Registration
from pytusk.core.types.tips import FROM_GAS

__all__ = [
    "CertificationOutcome",
    "execute_certify",
    "execute_registration_txn",
    "execute_reserve_and_register",
    "preflight_payment",
    "preflight_sponsor",
    "submit_certification",
]

_RESUME_HINT: str = (
    "The sliver fan-out had NOT yet run at this point, so no storage node "
    "holds this blob's slivers and none will sign a confirmation for it -- "
    "`tusky certify_blob` CANNOT recover this registration, because it only "
    "collects confirmations and submits Tx2, and has no source bytes to "
    "upload. Re-run the upload from the start; the paid storage on this "
    "registration is stranded."
)




async def execute_registration_txn(
    *,
    client: WalrusClient,
    txn: AsyncSuiTransaction,
    encoded: EncodedBlob,
    owner: str,
    label: str = "reserve_space/register_blob",
    finality_max_attempts: int = DEFAULT_FINALITY_MAX_ATTEMPTS,
    finality_max_delay: float = DEFAULT_FINALITY_MAX_DELAY,
) -> Registration:
    """Sign, submit, and read back a transaction that registers a blob.

    Shared tail for every Tx1 shape. The caller composes whatever PTB it
    needs -- registration alone, or registration bundled with an upload
    relay tip -- and hands the finished transaction here. Everything from
    signing through the ``Registration`` read-back is identical across
    those shapes and lives only here.

    The ordering below is load-bearing and must not be rearranged:
    ``end_epoch`` is never present in the transaction effects, so it costs
    a follow-up ``GetObject``, and that read is only valid once
    :func:`wait_for_finality` confirms the transaction reached a
    checkpoint. Reading the newly created object before then returns a
    stub.

    Args:
        client: Walrus client used to submit and read back.
        txn: Fully composed transaction. The caller is responsible for
            consuming the ``Blob`` result -- an unconsumed value aborts
            the build.
        encoded: Encoded blob whose ``blob_id`` lands in the result.
        owner: Address the created ``Blob`` is transferred to, used to
            pick the object out of the transaction effects.
        label: Operation name used in error messages, so a bundled PTB
            can report what it actually contained.
        finality_max_attempts: Maximum checkpoint-visibility polls.
        finality_max_delay: Maximum delay between those polls.

    Returns:
        The ``Registration`` describing the newly created blob object.

    Raises:
        RuntimeError: The transaction failed, or succeeded on-chain but
            could not be read back.
        RegistrationPendingError: The transaction succeeded but finality
            or read-back did not complete. The blob IS registered and
            storage IS paid for -- the digest and object id are carried
            on the error for resumption.
    """
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"{label} transaction failed: {result.result_string}")
    effects = require_success(result_data=result.result_data, label=label)

    try:
        object_id = find_created_object_id(effects=effects, owner=owner)
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
            stage="register_finality",
        )

    blob_result = await client.execute(command=GetObject(object_id=object_id))
    if not blob_result.is_ok():
        raise RegistrationPendingError(
            digest=result.result_data.digest,
            object_id=object_id,
            detail=f"Cannot fetch newly created Blob: {blob_result.result_string}",
            stage="register_readback",
        )
    try:
        # NOTE THE ORDER: this returns (deletable, end_epoch) -- the REVERSE
        # of the private copy that used to live in this module. Both elements
        # are truthy-typed, so a reversed unpack would run silently rather
        # than raise; it is spelled out here for the next reader.
        actual_deletable, end_epoch = blob_deletable_and_end_epoch(
            obj=blob_result.result_data
        )
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


async def preflight_sponsor(*, client: WalrusClient, sponsor: str | None = None) -> None:
    """Validate that ``sponsor`` is signable in the active PysuiConfiguration.

    A no-op when ``sponsor`` is ``None``. Otherwise verified via
    ``client.pysui_client.config.keypair_for_address``, used purely for its
    ``ValueError`` -- no keypair is retained.

    This exists because :mod:`~pytusk.core.pipelines.write`'s certify
    transaction (Tx2) is auto-signed internally and cannot be pre-built for
    external signing, so an unsignable sponsor must be caught before
    anything is spent. It is intentionally split out from
    :func:`preflight_payment` so it can be called FIRST, before any encode
    or network work -- unlike the tip-coin check, sponsor signability needs
    only config and has no dependency on a tip quote.

    Args:
        client (WalrusClient): Client used for the signability check.
        sponsor (str | None): Address sponsoring gas, or ``None`` to skip
            the check entirely.

    Raises:
        RelayUploadError: If ``sponsor`` is given and it is not signable in
            the active ``PysuiConfiguration``.
    """
    if sponsor is None:
        return
    try:
        client.pysui_client.config.keypair_for_address(address=sponsor)
    except ValueError as exc:
        raise RelayUploadError(
            message=(
                f"Sponsor {sponsor} is not signable in the active "
                "PysuiConfiguration. The encapsulated relay pipeline "
                "must be able to sign the certify transaction, which "
                "cannot be pre-built for external signing. Use the "
                "composable relay functions for external sponsors."
            ),
            stage="preflight",
        ) from exc


async def preflight_payment(
    *,
    client: WalrusClient,
    sender: str,
    sponsor: str | None = None,
    tip_source: str | None = None,
    tip_minimum_balance: int = 0,
    wal_payment_coin: str | None = None,
) -> str:
    """Validate tip-coin usability and resolve the WAL payment coin.

    Runs, in this fixed order:

    1. Tip-coin usability, when ``tip_source`` is given and is not
       :data:`~pytusk.core.types.FROM_GAS`: verified via
       :func:`~pytusk.core.ops.coins.assert_coin_usable` against ``sender``
       (and ``sponsor``, when given) as acceptable owners, requiring at
       least ``tip_minimum_balance``.
    2. WAL payment coin resolution: ``wal_payment_coin`` is returned as-is
       when given; otherwise one is selected via
       :func:`~pytusk.core.ops.coins.select_wal_payment_coin`.

    Unlike :func:`preflight_sponsor`, this cannot run before any network
    work: the tip-coin check needs the quote's ``tip_source``, which is
    only available after the tip has been quoted. This is the
    EXECUTE-layer home for guards that used to be hand-copied into every
    Tx1 assembly site -- extracted here so
    :mod:`~pytusk.core.pipelines.write` and
    :func:`execute_reserve_and_register` share exactly one implementation
    of each, rather than risking one caller's copy silently falling behind
    the other's (which is what had already happened to the ``tusky`` relay
    CLI's ``simulate`` mode before this function existed: it composed the
    same PTB but skipped the tip-coin check entirely).

    Args:
        client (WalrusClient): Client used for the coin fetch and WAL coin
            selection.
        sender (str): The already-resolved transaction sender -- the
            acceptable owner for a caller-supplied tip coin, and the
            ``owner`` passed to :func:`~pytusk.core.ops.coins.select_wal_payment_coin`.
        sponsor (str | None): Address sponsoring gas, or ``None``. Also an
            acceptable owner for a caller-supplied tip coin, since either
            party of a sponsored transaction may legitimately hold it.
        tip_source (str | None): Object ID of a tip coin to verify, or
            :data:`~pytusk.core.types.FROM_GAS`/``None`` to skip the check
            entirely -- the caller decides whether a tip is even in play
            (e.g. a relay that charges no tip) before calling this.
        tip_minimum_balance (int): Minimum acceptable balance, in MIST, for
            ``tip_source``. Ignored when the tip-coin check is skipped.
        wal_payment_coin (str | None): Object ID of a ``Coin<WAL>`` to use
            as payment. When ``None``, one is selected automatically.

    Returns:
        str: The WAL payment coin to use for
        :func:`~pytusk.core.ops.blob_compose.add_registration_sequence` --
        ``wal_payment_coin`` verbatim if given, otherwise one selected via
        :func:`~pytusk.core.ops.coins.select_wal_payment_coin`.

    Raises:
        RuntimeError: Propagated from :func:`~pytusk.core.ops.coins.assert_coin_usable`
            when ``tip_source`` is given, is not
            :data:`~pytusk.core.types.FROM_GAS`, and is unusable (wrong
            owner, insufficient balance, or unfetchable).
    """
    if tip_source is not None and tip_source != FROM_GAS:
        owners = {sender}
        if sponsor is not None:
            owners.add(sponsor)
        await assert_coin_usable(
            client=client,
            coin_id=tip_source,
            owners=owners,
            minimum_balance=tip_minimum_balance,
        )

    return wal_payment_coin or await select_wal_payment_coin(
        client=client, owner=sender
    )


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

    THIN WRAPPER around
    :func:`~pytusk.core.ops.blob_compose.add_registration_sequence` (with
    ``tip=None`` -- this path never has a relay tip) -- see that module's
    docstring for the "caller owns the transaction lifecycle" note. All PTB
    composition lives there; this function resolves the WAL payment coin via
    :func:`preflight_payment`, opens a transaction, delegates
    composition (which itself consumes the returned ``Blob`` result by
    transferring it to the resolved ``sender`` -- an object result left
    unconsumed by a later command would otherwise abort the PTB, exactly
    the reason ``delete_blob`` (``pytusk.tusky.tusky_cmds_lifecycle``) and
    ``exchange_for_wal`` (``pytusk.tusky.tusky_cmds_exchange``) transfer
    their own move_call results), then builds, signs, and submits it. It
    exists for a caller who wants the old one-call behaviour rather than
    composing Tx1 by hand.

    The ``Blob`` is ALWAYS transferred to the resolved ``sender`` -- there
    is deliberately no way to hand it to a different recipient here.
    ``certify_blob`` (Tx2) takes the blob as ``&mut Blob`` and must be
    signed by its current owner, so if Tx1 transferred the object to a
    third-party recipient, Tx2 would fail at simulation with "Transaction
    was not signed by the correct sender" once ``certify_blob`` tried to
    mutate an object it no longer owns. Handing the ``Blob`` to a different
    recipient is Tx2's job: pass ``recipient`` to
    :func:`execute_certify`/:func:`~pytusk.core.ops.blob_compose.add_certify`
    instead, which transfers it in the SAME PTB immediately after
    certification, so the sender still owns it when it is signed for and
    the eventual transfer is atomic with certification succeeding.

    ``payment_coin`` is OPTIONAL here (unlike the required
    ``wal_payment_coin`` argument on
    :func:`~pytusk.core.ops.blob_compose.add_registration_sequence`, which
    is client-free and cannot resolve a missing coin itself): when omitted,
    :func:`preflight_payment` falls back to
    :func:`~pytusk.core.ops.coins.select_wal_payment_coin` for backward
    compatibility with callers of the old one-call behaviour. An SDK
    developer who knows their own wallet's WAL coin type should pass
    ``payment_coin`` explicitly and skip that extra network round trip.

    ``sponsor`` is deliberately NOT validated via :func:`preflight_sponsor`
    here -- unlike the relay path, this Tx1 is built for the caller to sign
    (or hand to the sponsor for out-of-band signing) rather than being
    auto-signed internally, so a sponsor with no keypair in the active
    ``PysuiConfiguration`` is a legitimate pattern, not an error. Forcing
    the check would break that workflow, so it is intentionally skipped
    here; :func:`preflight_sponsor` remains defined for
    :mod:`~pytusk.core.pipelines.write`, where it IS required because
    that pipeline auto-signs the certify transaction internally.

    ``sender``/``sponsor`` are threaded into
    ``client.transaction(initial_sender=..., initial_sponsor=...)``,
    matching how ``burn_blob`` in ``pytusk.tusky.tusky_cmds_lifecycle``
    resolves and passes them. When omitted, ``sender`` defaults to the
    active address and no sponsor is used -- the same behaviour as the
    previous bare ``client.transaction()`` call.

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
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        system_object (str): Object ID of the configured Walrus System
            object.
        payment_coin (str | None): Object ID of a ``Coin<WAL>`` to use as
            payment. When ``None``, one is selected automatically via
            :func:`~pytusk.core.ops.coins.select_wal_payment_coin`.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Always the owner of the created ``Blob``
            -- see the transfer note above.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.
        finality_max_attempts (int): Poll budget handed to
            :func:`~pytusk.core.ops.system_reads.wait_for_finality` before the read-back. Defaults to
            :data:`~pytusk.core.ops.system_reads.DEFAULT_FINALITY_MAX_ATTEMPTS`.
        finality_max_delay (float): Per-attempt delay ceiling, in seconds,
            handed to :func:`~pytusk.core.ops.system_reads.wait_for_finality`. Defaults to
            :data:`~pytusk.core.ops.system_reads.DEFAULT_FINALITY_MAX_DELAY`.

    Returns:
        Registration: The recoverable checkpoint for Tx2.

    Raises:
        RuntimeError: If WAL coin selection, transaction submission, or
            on-chain execution fails (no digest available in this case --
            the transaction never produced usable effects). If Tx1
            SUCCEEDS on-chain but :func:`~pytusk.core.chain.effects.find_created_object_id` cannot
            locate the created ``Blob`` in the effects (an assumption
            UNVERIFIED against a live node -- see that function's
            docstring), the error is re-raised (chained via ``from``) with
            Tx1's transaction digest and :data:`_RESUME_HINT` appended. If
            Tx1 SUCCEEDS, the created ``Blob``'s object ID IS found, and
            checkpoint finality and the ``GetObject`` read-back both
            succeed, but ``blob_deletable_and_end_epoch`` then raises
            ``ValueError`` on a malformed/unexpected JSON view, the error
            is re-raised (chained via ``from``) with Tx1's transaction
            digest and the same hint.

            Note that NONE of these ``RuntimeError`` cases are recoverable
            with ``tusky certify_blob`` -- see :data:`_RESUME_HINT`. This is
            unlike :class:`~pytusk.core.types.errors.RegistrationPendingError`
            below, which IS recoverable.
        RegistrationPendingError: If Tx1 succeeds on-chain but checkpoint
            finality is not reached within ``finality_max_attempts``, or the
            immediate read-back of the newly created ``Blob`` via
            :class:`GetObject` fails. Unlike the ``RuntimeError`` cases
            above, this IS transient and recoverable: ``digest`` and
            ``object_id`` are valid and the pipeline can be resumed from
            sliver upload onward without re-executing Tx1 -- see this
            exception class's own docstring.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    resolved_payment_coin = await preflight_payment(
        client=client,
        sender=resolved_sender,
        sponsor=sponsor,
        wal_payment_coin=payment_coin,
    )

    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_registration_sequence(
        txn=txn,
        encoded=encoded,
        epochs=epochs,
        deletable=deletable,
        package_id=package_id,
        system_object=system_object,
        recipient=resolved_sender,
        wal_payment_coin=resolved_payment_coin,
        tip=None,
    )
    return await execute_registration_txn(
        client=client,
        txn=txn,
        encoded=encoded,
        owner=resolved_sender,
        finality_max_attempts=finality_max_attempts,
        finality_max_delay=finality_max_delay,
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

    THIN WRAPPER around :func:`~pytusk.core.ops.blob_compose.add_certify`
    -- see that module docstring's "caller owns the transaction lifecycle"
    note. All PTB composition lives there; this function only opens a
    transaction, delegates to it, then builds, signs, and submits.
    ``certify_blob`` has no return value (it mutates the referenced
    ``Blob`` in place), so unlike Tx1 there is no command result to
    consume/transfer -- unless ``recipient`` is given, in which case
    :func:`~pytusk.core.ops.blob_compose.add_certify` appends a
    ``transfer_objects`` command after ``certify_blob`` in the same PTB
    (see its docstring).

    ``sender``/``sponsor`` are threaded into
    ``client.transaction(initial_sender=..., initial_sponsor=...)``,
    matching how ``burn_blob`` in ``pytusk.tusky.tusky_cmds_lifecycle``
    resolves and passes them. When omitted, ``sender`` defaults to the
    active address and no sponsor is used -- the same behaviour as the
    previous bare ``client.transaction()`` call.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        registration (Registration): Tx1's result, identifying the Blob to
            certify.
        certificate (Certificate): The quorum-backed certificate to submit.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
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
            :func:`~pytusk.core.ops.blob_compose.add_certify`. When
            ``None`` (the default), no transfer is added and the ``Blob``
            stays with ``sender``.

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
    require_success(result_data=result.result_data, label="certify_blob")

    return CertifyResult(
        object_id=registration.object_id,
        blob_id=registration.blob_id,
        certified=True,
        digest=result.result_data.digest,
    )


@dataclasses.dataclass(kw_only=True, frozen=True)
class CertificationOutcome:
    """Tx2's result together with how long submitting it took.

    Pairs :class:`~pytusk.core.types.receipts.CertifyResult` with the stage
    duration a pipeline needs for its receipt's ``timings``, so a caller does
    not time the call itself -- two callers timing the same work would
    otherwise be free to measure different spans of it.

    Attributes:
        result (CertifyResult): The outcome of Tx2.
        duration (float): Wall-clock seconds spent in the
            :func:`execute_certify` call, measured with
            :func:`time.monotonic`.
    """

    result: CertifyResult
    duration: float


async def submit_certification(
    *,
    client: WalrusClient,
    committee: WalrusCommittee,
    registration: Registration,
    certificate: Certificate,
    package_id: str,
    system_object: str,
    sender: str | None = None,
    sponsor: str | None = None,
    recipient: str | None = None,
) -> CertificationOutcome:
    """Verify a certificate locally, then submit Tx2 and time it.

    The shared Tx2 stage for every write path. It is deliberately
    RECEIPT-FREE: it returns the transaction's own result and duration and
    builds no user-facing receipt, because the receipt each path returns is
    path-specific while this work is not. That separation is what lets a
    second pipeline reuse this stage without inheriting the first pipeline's
    receipt shape.

    :func:`~pytusk.core.certification.verify_certificate` runs BEFORE
    anything is submitted, so a certificate that does not verify against the
    committee's public keys fails locally and for free rather than aborting
    on-chain after gas has been spent.

    This does NOT check that ``committee.epoch`` is still current. Signer
    positions are ordering-dependent and a committee reorder invalidates a
    certificate, but the RECOVERY from that differs per path: the native path
    can refetch the committee and re-collect confirmations (see
    :func:`~pytusk.core.native_upload.certify.certify`), whereas a relay
    caller holds no confirmations of its own to re-collect. Freshness is
    therefore the caller's responsibility --
    :func:`~pytusk.core.native_upload.assert_certificate_epoch_current` is
    the ready-made check for a caller that wants one.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        committee (WalrusCommittee): The committee ``certificate`` was built
            against, used to look up the signers' public keys for local
            verification.
        registration (Registration): Tx1's result, identifying the ``Blob``
            to certify.
        certificate (Certificate): The quorum-backed certificate to submit.
        package_id (str): Walrus package ID.
        system_object (str): Object ID of the configured Walrus System
            object.
        sender (str | None): Address to sign Tx2 as. Defaults to the active
            address when ``None``. Must be the current owner of the ``Blob``.
        sponsor (str | None): Address to sponsor Tx2's gas as, or ``None``
            for no sponsorship.
        recipient (str | None): Sui address to transfer the certified
            ``Blob`` to in the same PTB, immediately after ``certify_blob``.
            ``None`` leaves it with ``sender``.

    Returns:
        CertificationOutcome: Tx2's result and the duration of the submit.

    Raises:
        CertifyTransactionError: If local certificate verification fails --
            with ``duration`` left ``None``, since nothing was submitted --
            or if :func:`execute_certify` fails to submit, aborts on-chain,
            or fails pysui's pre-submission gas-estimation dry run. pysui
            signals those last two with TWO different bare exception types:
            ``RuntimeError`` for a submission failure or on-chain abort, and
            ``ValueError`` from its ``txn_gas.py`` for a simulate failure
            inside ``txn.build_and_sign()``. Both are converted here with the
            original message preserved and this call's own duration attached.
    """
    signer_public_keys = [
        committee.members[position].public_key
        for position in certificate.signer_positions
    ]
    if not verify_certificate(certificate=certificate, public_keys=signer_public_keys):
        raise CertifyTransactionError(
            message=(
                "Local certificate verification failed -- the aggregate "
                "signature does not verify against the committee public "
                "keys for its signer positions; refusing to submit Tx2"
            ),
            stage="certify",
        )

    certify_tx2_start = time.monotonic()
    try:
        result = await execute_certify(
            client=client,
            registration=registration,
            certificate=certificate,
            package_id=package_id,
            system_object=system_object,
            sender=sender,
            sponsor=sponsor,
            recipient=recipient,
        )
    except (RuntimeError, ValueError) as exc:
        raise CertifyTransactionError(
            message=str(exc),
            stage="certify",
            duration=time.monotonic() - certify_tx2_start,
        ) from exc
    return CertificationOutcome(
        result=result, duration=time.monotonic() - certify_tx2_start
    )
