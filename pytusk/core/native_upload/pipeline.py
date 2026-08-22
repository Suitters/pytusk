#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""End-to-end compose of the native upload pipeline.

See :mod:`pytusk.core.native_upload` (the package's ``__init__.py``) for the
full native upload pipeline description and stage ordering.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import time

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.encoding import encode_blob
from pytusk.core.native_upload.certify import certify
from pytusk.core.native_upload.common import (
    NativeBlobReceipt,
    NativeUploadError,
    StageTimings,
)
from pytusk.core.native_upload.confirm import collect_confirmations
from pytusk.core.native_upload.fanout import upload_slivers
from pytusk.core.system_ops import execute_reserve_and_register
from pytusk.core.utils import resolve_package_id


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
    """Run the full native upload pipeline: encode, register, upload, certify.

    Thin compose only -- this orchestrates :func:`~pytusk.core.encoding.encode_blob`,
    :func:`~pytusk.core.system_ops.execute_reserve_and_register`,
    :func:`upload_slivers`, :func:`collect_confirmations` and :func:`certify`
    in the fixed order documented in the module docstring; it contains no
    logic of its own.

    ``client.config.network.system_object`` is the Walrus System object ID
    field defined on ``WalrusNetworkConfig`` in ``pytusk.config.tusk_config``
    (populated for both built-in networks in that module's
    ``_DEFAULT_NETWORKS``), sitting alongside the already-verified
    ``client.config.network.staking_object`` used by
    :meth:`~pytusk.client.walrus_client.WalrusClient.walrus_epoch` and
    :meth:`~pytusk.client.walrus_client.WalrusClient.committee`.

    On any :class:`NativeUploadError` raised after registration has already
    succeeded (i.e. from :func:`upload_slivers`, :func:`collect_confirmations`
    or :func:`certify`), this returns a :class:`NativeBlobReceipt` with
    ``certified=False`` and ``failed_stage`` set, rather than raising --
    ``registration`` already represents WAL spent and a real on-chain Blob
    object, which is worth reporting back rather than discarding into an
    exception. A failure before registration (e.g. from ``encode_blob`` or
    Tx1 itself) has no such state to report and propagates normally.

    :func:`collect_confirmations` is called here with NO ``positions``
    restriction, i.e. its default of querying every committee member --
    see that function's docstring for why a failed sliver upload must not
    be used to pre-exclude a node from confirmation collection.
    ``max_confirmation_requests`` therefore only reaches
    :func:`collect_confirmations` now; :func:`upload_slivers`'s own
    concurrency is governed by its byte-throttle and per-node-connection
    parameters instead (see its docstring), which this compose function
    leaves at their upstream-aligned defaults rather than re-exposing here.

    ``sender``/``sponsor``/``payment_coin`` are threaded straight through to
    :func:`~pytusk.core.system_ops.execute_reserve_and_register` (Tx1); see
    its docstring for their defaulting behaviour. ``recipient`` is
    DELIBERATELY NOT passed to Tx1 -- Tx1 always transfers the newly
    registered ``Blob`` to the resolved sender, because ``certify_blob``
    (Tx2) requires the signer to own the object it mutates. Instead,
    ``recipient`` is threaded into the :func:`certify` (Tx2) call, which
    transfers the ``Blob`` to it in the SAME PTB, immediately after
    certification succeeds -- atomic with certification, and never handing
    the object away before the sender needs to sign for it. Tx2 is
    otherwise still submitted under the active address with no sponsor --
    this compose function does not thread ``sender``/``sponsor`` into
    :func:`certify`.

    Args:
        client (WalrusClient): Client used for every stage.
        data (bytes): The blob content to store.
        epochs (int): Number of epochs ahead to reserve storage for.
        deletable (bool): Whether the stored blob should be deletable.
        max_confirmation_requests (int): Maximum concurrent confirmation
            requests, passed through to :func:`collect_confirmations`.
        wait_millis (int | None): Upper bound in milliseconds for each
            node's confirmation long-poll wait.
        sender (str | None): Address to sign Tx1 as. Defaults to the active
            address when ``None``. Always the owner of the created ``Blob``
            after Tx1 -- see the note above on why ``recipient`` is not
            threaded into Tx1.
        sponsor (str | None): Address to sponsor Tx1's gas as, or ``None``
            for no sponsorship.
        payment_coin (str | None): Object ID of a ``Coin<WAL>`` to use as
            Tx1 payment. When ``None``, one is selected automatically.
        recipient (str | None): Sui address to transfer the certified
            ``Blob`` to, as part of Tx2 (see the note above). Treated as a
            valid Sui address and used verbatim -- NOT validated. When
            ``None``, the ``Blob`` stays with the resolved ``sender``.

    Returns:
        NativeBlobReceipt: The outcome of the upload attempt, with
            ``timings`` (a :class:`StageTimings`) populated for every stage
            that ran -- on both the fully-certified and the partial/failed
            path.
    """
    pipeline_start = time.monotonic()

    committee = await client.committee()

    encode_start = time.monotonic()
    encoded = await asyncio.to_thread(
        functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
    )
    encode_duration = time.monotonic() - encode_start

    system_object = client.config.network.system_object
    staking_object = client.config.network.staking_object
    package_id = await resolve_package_id(client=client, system_object=system_object)

    register_tx1_start = time.monotonic()
    registration = await execute_reserve_and_register(
        client=client,
        encoded=encoded,
        epochs=epochs,
        deletable=deletable,
        package_id=package_id,
        system_object=system_object,
        payment_coin=payment_coin,
        sender=sender,
        sponsor=sponsor,
    )
    register_tx1_duration = time.monotonic() - register_tx1_start

    sliver_upload_duration: float | None = None
    confirmations_duration: float | None = None
    try:
        sliver_upload_start = time.monotonic()
        try:
            await upload_slivers(
                client=client,
                committee=committee,
                encoded=encoded,
            )
        finally:
            sliver_upload_duration = time.monotonic() - sliver_upload_start

        confirmations_start = time.monotonic()
        try:
            certificate = await collect_confirmations(
                client=client,
                committee=committee,
                blob_id=encoded.blob_id,
                registration=registration,
                wait_millis=wait_millis,
                max_confirmation_requests=max_confirmation_requests,
            )
        finally:
            confirmations_duration = time.monotonic() - confirmations_start

        receipt = await certify(
            client=client,
            committee=committee,
            blob_id=encoded.blob_id,
            registration=registration,
            certificate=certificate,
            package_id=package_id,
            system_object=system_object,
            staking_object=staking_object,
            recipient=recipient,
            stage_timings=StageTimings(
                encode=encode_duration,
                register_tx1=register_tx1_duration,
                sliver_upload=sliver_upload_duration,
                confirmations=confirmations_duration,
                certify_tx2=None,
                total=None,
            ),
        )
        total_duration = time.monotonic() - pipeline_start
        return dataclasses.replace(
            receipt,
            timings=dataclasses.replace(receipt.timings, total=total_duration),
        )
    except NativeUploadError as exc:
        total_duration = time.monotonic() - pipeline_start
        return NativeBlobReceipt(
            blob_id=encoded.blob_id_base64,
            object_id=registration.object_id,
            certified=False,
            end_epoch=registration.end_epoch,
            failed_stage=exc.stage,
            timings=StageTimings(
                encode=encode_duration,
                register_tx1=register_tx1_duration,
                sliver_upload=sliver_upload_duration,
                confirmations=confirmations_duration,
                # exc.duration is None for every NativeUploadError raised
                # before Tx2 is attempted (SliverUploadError,
                # ConfirmationCollectionError, EpochMismatchError); only
                # CertifyTransactionError sets it, to the certify_tx2
                # duration certify()'s own try/finally already recorded
                # before re-raising -- see CertifyTransactionError's
                # docstring.
                certify_tx2=exc.duration,
                total=total_duration,
            ),
        )
