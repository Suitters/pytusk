#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""The relay POST stage, with its retry budget.

The retry loop lives HERE rather than in the pipeline because
:func:`upload_to_relay` is a public composable entry point whose main
standalone use is resuming a paid-but-unfinished upload. A resuming caller
must not have to reimplement the retry rule to avoid re-POSTing wrongly.
"""

import asyncio
import time

from pytusk.client.walrus_client import WalrusClient
from pytusk.commands.relay_commands import UploadRelayBlob
from pytusk.core.types import RelayUploadOutcome, RelayUploadResult

__all__ = [
    "DEFAULT_MAX_UPLOAD_ATTEMPTS",
    "upload_to_relay",
]

DEFAULT_MAX_UPLOAD_ATTEMPTS: int = 5
"""Default number of POSTs attempted before reporting UNANSWERED."""

_RETRY_BASE_DELAY: float = 0.5
"""First backoff delay, in seconds; doubles per retry."""

_RETRY_MAX_DELAY: float = 8.0
"""Ceiling on the backoff delay, in seconds."""


async def upload_to_relay(
    *,
    client: WalrusClient,
    relay_url: str,
    blob_id: str,
    data: bytes,
    register_tip_tx_digest: str | None = None,
    nonce: str | None = None,
    deletable_blob_object: str | None = None,
    max_attempts: int = DEFAULT_MAX_UPLOAD_ATTEMPTS,
) -> RelayUploadResult:
    """POST a blob to a relay, retrying only what is safe to retry.

    Retries transport failures and 5xx. NEVER retries 400/401/402: those
    are deterministic, and a fixed paid tip cannot satisfy a 402.

    Retrying is safe because the relay implements NO replay protection --
    re-POSTing the identical ``tx_id`` and ``nonce`` is accepted and never
    re-charges. Every attempt therefore reuses the same tokens; generating
    a fresh nonce would abandon the tip already paid.

    Reports rather than raises. A budget exhausted without an answer is
    :attr:`RelayUploadOutcome.UNANSWERED`, NOT an error -- the tip is
    untouched and the returned tokens make a later POST free.

    Args:
        client (WalrusClient): Client used to issue the POST.
        relay_url (str): Base URL of the relay.
        blob_id (str): Blob ID as URL-safe unpadded base64.
        data (bytes): Raw UNENCODED blob bytes.
        register_tip_tx_digest (str | None): Digest of the tip transaction,
            sent as ``tx_id``. Omit only against a ``no_tip`` relay.
        nonce (str | None): Base64url nonce from the authentication
            package. Omit only against a ``no_tip`` relay.
        deletable_blob_object (str | None): Object ID when the blob was
            registered deletable; omitted means permanent.
        max_attempts (int): POST attempt budget, at least 1.

    Returns:
        RelayUploadResult: The certificate, or the points needed to resume.

    Raises:
        ValueError: If ``max_attempts`` is less than 1. A caller-side
            contract violation, detected before any request is made.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")

    command = UploadRelayBlob(
        blob_id=blob_id,
        data=data,
        register_tip_tx_digest=register_tip_tx_digest,
        nonce=nonce,
        deletable_blob_object=deletable_blob_object,
    )
    started = time.monotonic()
    delay = _RETRY_BASE_DELAY
    transport_error: str | None = None
    relay_status: int | None = None
    relay_message: str | None = None
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        result = await client.execute(command=command, base_url=relay_url)
        ack = result.result_data

        if result.is_ok():
            return RelayUploadResult(
                outcome=RelayUploadOutcome.UPLOADED,
                certificate=ack.confirmation_certificate,
                blob_id=blob_id,
                relay_url=relay_url,
                register_tip_tx_digest=register_tip_tx_digest,
                nonce=nonce,
                relay_status=ack.relay_status,
                relay_message=None,
                transport_error=None,
                attempts=attempts,
                duration=time.monotonic() - started,
            )

        if ack is None:
            # No RelayUploadAck means no HTTP response ever arrived --
            # WalrusClient._send converts every transport failure into a
            # two-argument failure result. Always retryable.
            transport_error = result.result_string
            relay_status = None
            relay_message = None
        else:
            transport_error = None
            relay_status = ack.relay_status
            relay_message = ack.relay_message
            if relay_status is None or relay_status < 500:
                return RelayUploadResult(
                    outcome=RelayUploadOutcome.REFUSED,
                    certificate=None,
                    blob_id=blob_id,
                    relay_url=relay_url,
                    register_tip_tx_digest=register_tip_tx_digest,
                    nonce=nonce,
                    relay_status=relay_status,
                    relay_message=relay_message,
                    transport_error=None,
                    attempts=attempts,
                    duration=time.monotonic() - started,
                )

        if attempt < max_attempts:
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY)

    return RelayUploadResult(
        outcome=RelayUploadOutcome.UNANSWERED,
        certificate=None,
        blob_id=blob_id,
        relay_url=relay_url,
        register_tip_tx_digest=register_tip_tx_digest,
        nonce=nonce,
        relay_status=relay_status,
        relay_message=relay_message,
        transport_error=transport_error,
        attempts=attempts,
        duration=time.monotonic() - started,
    )
