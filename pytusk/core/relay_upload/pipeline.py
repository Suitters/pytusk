#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Encapsulated store-via-upload-relay pipeline.

This is the config-driven, execute-only entry point: hand it bytes and an
epoch count and it registers, tips, uploads, and certifies. Callers who
need URL-level or transaction-level control use the composable functions
in this package directly.

The stage order is forced by the relay protocol and cannot be rearranged:

    encode -> committee -> tip quote -> [tip + register, ONE PTB] ->
    POST (with retry) -> parse certificate -> certify

The blob MUST be registered before the POST (the relay answers 401
``BlobIdNotRegistered`` otherwise), and the tip MUST be settled before the
POST because the relay independently re-verifies the payment on chain.
Certification can never share a PTB with the tip: its input is the
certificate returned by the POST, which does not exist until the tip
transaction has landed.

Failures are reported, not raised. Once the tip is paid and the blob is
registered, every later failure returns a ``RelayBlobReceipt`` whose
``outcome`` is ``RESUMABLE`` and which carries the resumption tokens --
the transaction digest, the blob id, and the base64url nonce. Raising
would discard exactly the values a caller needs to recover. Exceptions
here are reserved for contract violations detected before anything is
spent.
"""

from __future__ import annotations

import asyncio
import functools
import time

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.encoding import encode_blob
from pytusk.core.relay_types import RelayUploadOutcome
from pytusk.core.relay_upload.common import (
    RelayBlobReceipt,
    RelayOutcome,
    RelayStageTimings,
    RelayUploadError,
)
from pytusk.core.relay_upload.relay_certify import parse_relay_certificate
from pytusk.core.relay_upload.tip import (
    FROM_GAS,
    add_tip,
    build_auth_package,
    quote_tip,
)
from pytusk.core.relay_upload.upload import (
    DEFAULT_MAX_UPLOAD_ATTEMPTS,
    upload_to_relay,
)
from pytusk.core.system_ops import (
    add_reserve_and_register,
    execute_certify,
    execute_registration_txn,
)
from pytusk.core.utils import (
    assert_coin_usable,
    resolve_package_id,
    select_wal_payment_coin,
)

__all__ = ["store_blob_relay"]


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
            from the register transaction and the certificate from the
            POST).
        recipient: Address the certified ``Blob`` is transferred to,
            atomically with certification. ``None`` leaves it with sender.
        tip_source: ``"from_gas"`` to split the tip from the gas coin --
            whoever funds the transaction pays -- or a coin object id to
            split from. A coin id is verified against the resolved sender
            and sponsor before anything is built.
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
    if sponsor is not None:
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

    relay_url = client.config.relay_url_for(
        network_name=client.config.active_network, relay_name=relay_name
    )

    committee = await client.committee()

    encode_start = time.monotonic()
    encoded = await asyncio.to_thread(
        functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
    )
    encode_duration = time.monotonic() - encode_start

    tip_config_start = time.monotonic()
    quote = await quote_tip(
        client=client,
        relay_url=relay_url,
        unencoded_length=len(data),
        n_shards=committee.n_shards,
    )
    tip_config_duration = time.monotonic() - tip_config_start

    auth_package = build_auth_package(data=data) if quote.requires_payment else None
    if quote.requires_payment and tip_source != FROM_GAS:
        owners = {resolved_sender}
        if sponsor is not None:
            owners.add(sponsor)
        await assert_coin_usable(
            client=client,
            coin_id=tip_source,
            owners=owners,
            minimum_balance=quote.amount or 0,
        )

    system_object = client.config.network.system_object
    package_id = await resolve_package_id(client=client, system_object=system_object)
    resolved_wal_coin = wal_payment_coin or await select_wal_payment_coin(
        client=client, owner=resolved_sender
    )

    register_start = time.monotonic()
    txn = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    if auth_package is not None:
        await add_tip(
            txn=txn,
            relay_address=quote.address or "",
            tip_amount=quote.amount or 0,
            auth_package=auth_package,
            payment_coin=tip_source,
        )
    blob = await add_reserve_and_register(
        txn=txn,
        package_id=package_id,
        system_object=system_object,
        encoded=encoded,
        epochs=epochs,
        deletable=deletable,
        payment_coin=resolved_wal_coin,
    )
    await txn.transfer_objects(transfers=[blob], recipient=resolved_sender)
    registration = await execute_registration_txn(
        client=client,
        txn=txn,
        encoded=encoded,
        owner=resolved_sender,
        label=(
            "tip/reserve_space/register_blob"
            if auth_package is not None
            else "reserve_space/register_blob"
        ),
    )
    register_duration = time.monotonic() - register_start

    nonce = auth_package.nonce_base64url if auth_package is not None else None
    tip_paid = auth_package is not None

    upload = await upload_to_relay(
        client=client,
        relay_url=relay_url,
        blob_id=encoded.blob_id_base64,
        data=data,
        register_tip_tx_digest=registration.digest if tip_paid else None,
        nonce=nonce,
        deletable_blob_object=registration.object_id if deletable else None,
        max_attempts=max_upload_attempts,
    )

    if upload.outcome is not RelayUploadOutcome.UPLOADED or upload.certificate is None:
        return RelayBlobReceipt(
            outcome=(
                RelayOutcome.REJECTED
                if upload.outcome is RelayUploadOutcome.REFUSED
                else RelayOutcome.RESUMABLE
            ),
            blob_id=encoded.blob_id_base64,
            object_id=registration.object_id,
            certified=False,
            end_epoch=registration.end_epoch,
            failed_stage="relay_upload",
            timings=RelayStageTimings(
                encode=encode_duration,
                tip_config=tip_config_duration,
                register_tip_tx=register_duration,
                relay_upload=upload.duration,
                certify_tx=None,
                total=time.monotonic() - pipeline_start,
            ),
            relay_url=relay_url,
            tip_paid=tip_paid,
            tip_amount=quote.amount,
            register_tip_tx_digest=registration.digest,
            certify_tx_digest=None,
            nonce=nonce,
            relay_status=upload.relay_status,
            relay_message=upload.relay_message,
            attempts=upload.attempts,
            transport_error=upload.transport_error,
        )

    certify_start = time.monotonic()
    try:
        certificate = parse_relay_certificate(
            payload=upload.certificate, committee=committee
        )
        certify_result = await execute_certify(
            client=client,
            registration=registration,
            certificate=certificate,
            package_id=package_id,
            system_object=system_object,
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
            register_tip_tx_digest=registration.digest,
            certify_tx_digest=None,
            nonce=nonce,
            relay_status=upload.relay_status,
            relay_message=str(exc),
            attempts=upload.attempts,
            transport_error=None,
        )
    certify_duration = time.monotonic() - certify_start

    return RelayBlobReceipt(
        outcome=RelayOutcome.CERTIFIED,
        blob_id=encoded.blob_id_base64,
        object_id=certify_result.object_id,
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
        register_tip_tx_digest=registration.digest,
        certify_tx_digest=certify_result.digest,
        nonce=nonce,
        relay_status=upload.relay_status,
        relay_message=upload.relay_message,
        attempts=upload.attempts,
        transport_error=None,
    )
