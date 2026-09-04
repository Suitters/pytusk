#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Certify (Tx2) stage of the native upload pipeline.

See :mod:`pytusk.core.native_upload` (the package's ``__init__.py``) for the
full native upload pipeline description and stage ordering.
"""

import dataclasses

from pytusk.core.certification import Certificate
from pytusk.core.chain import WalrusCommittee, fetch_committee, fetch_epoch
from pytusk.core.encoding import blob_id_to_url_base64
from pytusk.core.native_upload.confirm import collect_confirmations
from pytusk.core.ops.blob_execute import submit_certification
from pytusk.core.types import (
    CertifyTransactionError,
    EpochMismatchError,
    NativeBlobReceipt,
    Registration,
    StageTimings,
)
from pytusk.core.types.protocols import ExecuteOnlyClient


async def assert_certificate_epoch_current(
    *, client: ExecuteOnlyClient, committee: WalrusCommittee, staking_object: str
) -> None:
    """Raise if the live on-chain epoch has moved off ``committee.epoch``.

    A :class:`~pytusk.core.certification.Certificate`'s ``signers_bitmap``
    positions are meaningful only against the committee ordering it was
    built from (see :class:`~pytusk.core.certification.Certificate`'s
    docstring). If the Walrus epoch advances between when confirmations
    were collected and when Tx2 is submitted, the committee may have
    reordered and the certificate's bitmap positions no longer identify the
    same nodes -- ``certify_blob`` would abort. This performs a single
    cheap epoch read (:func:`~pytusk.core.chain.committee.fetch_epoch`) so a
    caller composing Tx2 by hand via
    :func:`~pytusk.core.ops.blob_compose.add_certify` can check freshness
    before submitting, rather than discovering the mismatch from an
    on-chain abort after gas is spent.

    This is a CHECK ONLY -- unlike :func:`certify` (see its docstring), it
    does not refetch the committee, re-collect confirmations, or rebuild a
    certificate on a mismatch. A caller composing Tx2 by hand is
    responsible for redoing that work themselves if this raises;
    :func:`certify`'s existing retry behaviour is unchanged and remains the
    recommended path for callers who want that handled automatically.

    Args:
        client (WalrusClient): Client used to read the live epoch.
        committee (WalrusCommittee): The committee ``certificate`` (the one
            about to be submitted via
            :func:`~pytusk.core.ops.blob_compose.add_certify`) was built
            against.
        staking_object (str): Object ID of the configured Walrus staking
            object.

    Raises:
        EpochMismatchError: If the live on-chain epoch differs from
            ``committee.epoch``.
        RuntimeError: Propagated from
            :func:`~pytusk.core.chain.committee.fetch_epoch` if the epoch
            cannot be read.
    """
    current_epoch = await fetch_epoch(reader=client, staking_object=staking_object)
    if current_epoch != committee.epoch:
        raise EpochMismatchError(
            message=(
                f"On-chain epoch {current_epoch} differs from the committee "
                f"epoch {committee.epoch}; a certificate built against the "
                "latter's signer-bitmap ordering is no longer valid for "
                "certify_blob."
            ),
            stage="certify",
        )


async def certify(
    *,
    client: ExecuteOnlyClient,
    committee: WalrusCommittee,
    blob_id: bytes,
    registration: Registration,
    certificate: Certificate,
    package_id: str,
    system_object: str,
    staking_object: str,
    sender: str | None = None,
    sponsor: str | None = None,
    recipient: str | None = None,
    max_attempts: int = 2,
    stage_timings: StageTimings | None = None,
) -> NativeBlobReceipt:
    """Submit Tx2 (``certify_blob``), refetching on an epoch mismatch.

    Before submitting, the current on-chain epoch is re-read via the cheap
    :func:`~pytusk.core.chain.committee.fetch_epoch`. If it differs from
    ``committee.epoch``, that is treated as REFETCH-AND-RETRY, not a
    signature failure: the committee is refetched, confirmations are
    re-collected via :func:`collect_confirmations`, and the bitmap is
    REBUILT against the new committee ordering. The old certificate is
    never reused across an epoch change -- signer positions are ordering
    dependent, and ordering changes with the committee.

    Args:
        client (WalrusClient): Client used to submit the transaction and
            query the epoch/committee.
        committee (WalrusCommittee): The committee ``certificate`` was
            built against.
        blob_id (bytes): Raw 32-byte Walrus blob ID being certified. The
            base64 form used in the returned receipt is derived from this
            via :func:`~pytusk.core.encoding.blob_id_to_url_base64`.
        registration (Registration): Tx1's result, identifying the Blob.
        certificate (Certificate): The quorum-backed certificate to submit.
        package_id (str): Walrus package ID.
        system_object (str): Object ID of the configured Walrus System
            object.
        staking_object (str): Object ID of the configured Walrus staking
            object, used to re-read the epoch/committee on a mismatch.
        sender (str | None): Address to sign Tx2 as. Defaults to the active
            address when ``None``. Must be the current owner of the ``Blob``
            being certified -- see
            :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register`'s
            docstring for why Tx1 always transfers it there.
        sponsor (str | None): Address to sponsor Tx2's gas as, or ``None``
            for no sponsorship.
        recipient (str | None): Sui address to transfer the now-certified
            ``Blob`` to, in the same PTB, immediately after
            ``certify_blob`` -- passed straight through to
            :func:`~pytusk.core.ops.blob_execute.execute_certify`. When ``None``
            (the default), no transfer happens and the ``Blob`` stays with
            ``sender``.
        max_attempts (int): Maximum number of epoch checks before giving up.
        stage_timings (StageTimings | None): Durations already recorded for
            earlier pipeline stages (``encode``, ``register_tx1``,
            ``sliver_upload``, ``confirmations``) by the caller, carried
            through into the returned receipt's ``timings`` alongside this
            call's own ``certify_tx2`` duration. ``None`` (the default)
            starts from a :class:`StageTimings` with every field ``None``,
            for a caller (e.g. a standalone ``tusky certify_blob`` recovery
            run) that has no earlier stages to report.

    Returns:
        NativeBlobReceipt: The certified blob's receipt.

    Raises:
        EpochMismatchError: If the on-chain epoch still disagrees with the
            committee after ``max_attempts`` checks.
        CertifyTransactionError: If Tx2 (``certify_blob``) fails to submit,
            aborts on-chain, or fails pysui's pre-submission gas-estimation
            dry run. pysui signals these with TWO DIFFERENT exception
            types, and both must be caught here: a submission failure or
            on-chain abort inside :func:`~pytusk.core.ops.blob_execute.execute_certify`
            itself raises a bare ``RuntimeError``, while a gas-estimation/
            simulate failure inside ``txn.build_and_sign()`` -- which
            ``execute_certify`` calls before ever submitting -- raises
            ``ValueError`` from pysui's ``txn_gas.py``
            (``ValueError(f"Error running SimulateTransactionKind: ...")``).
            Both are converted here with the original message preserved and
            this call's own ``certify_tx2`` duration attached as
            ``duration``. ``NativeUploadError`` (this exception's own base
            class) is a ``RuntimeError`` subclass, but ``execute_certify``
            cannot itself raise one -- ``pytusk.core.ops`` deliberately has no
            dependency on ``native_upload`` (see that module's docstring) --
            so this ``except`` clause cannot double-wrap an
            already-wrapped ``NativeUploadError``. Also raised, with
            ``duration=None`` (Tx2 timing has not started yet), if
            :func:`~pytusk.core.certification.verify_certificate` finds the
            certificate's aggregate signature does not verify against the
            committee's public keys for its signer positions -- this local
            check runs before Tx2 is ever attempted, so a bad certificate
            fails for free instead of spending gas on-chain.
    """
    incoming_timings = stage_timings or StageTimings(
        encode=None,
        register_tx1=None,
        sliver_upload=None,
        confirmations=None,
        certify_tx2=None,
        total=None,
    )
    current_committee = committee
    current_certificate = certificate
    attempts = 0

    while True:
        attempts += 1
        current_epoch = await fetch_epoch(reader=client, staking_object=staking_object)
        if current_epoch == current_committee.epoch:
            break
        if attempts >= max_attempts:
            raise EpochMismatchError(
                message=(
                    f"On-chain epoch {current_epoch} still differs from the "
                    f"committee epoch {current_committee.epoch} after "
                    f"{attempts} attempt(s); giving up."
                ),
                stage="certify",
            )
        current_committee = await fetch_committee(
            reader=client, staking_object=staking_object
        )
        current_certificate = await collect_confirmations(
            client=client,
            committee=current_committee,
            blob_id=blob_id,
            registration=registration,
        )

    outcome = await submit_certification(
        client=client,
        committee=current_committee,
        registration=registration,
        certificate=current_certificate,
        package_id=package_id,
        system_object=system_object,
        error_type=CertifyTransactionError,
        sender=sender,
        sponsor=sponsor,
        recipient=recipient,
    )
    return NativeBlobReceipt(
        blob_id=blob_id_to_url_base64(blob_id=blob_id),
        object_id=outcome.result.object_id,
        certified=outcome.result.certified,
        end_epoch=registration.end_epoch,
        failed_stage=None,
        timings=dataclasses.replace(
            incoming_timings, certify_tx2=outcome.duration
        ),
        register_tx_digest=registration.digest,
        certify_tx_digest=outcome.result.digest,
    )
