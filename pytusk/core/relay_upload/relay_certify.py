#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Conversion of a relay's confirmation certificate into pytusk's own.

The relay's certificate is the ONLY place in pytusk where three fields of
one object arrive in three different encodings, which is why this lives in
its own module rather than inline in the pipeline.
"""

import binascii

from pytusk.core.certification import (
    Certificate,
    confirmation_message,
    is_quorum,
    pack_signers_bitmap,
)
from pytusk.core.chain import WalrusCommittee
from pytusk.core.encoding import decode_standard_base64
from pytusk.core.types import RelayCertificateParseError

__all__ = [
    "parse_relay_certificate",
]

_STAGE = "parse_relay_certificate"

_EPOCH_OFFSET: int = 3
"""Byte offset of the epoch inside a BCS-encoded Confirmation.

The layout is intent(3) ++ epoch(u32 LE) ++ blob_id(32) ++ persistence,
where persistence is 0x00 for a permanent blob or 0x01 ++ object_id(32)
for a deletable one.
"""


def parse_relay_certificate(
    *,
    payload: dict,
    committee: WalrusCommittee,
    blob_id: bytes,
    object_id: bytes | None,
) -> Certificate:
    """Convert a relay's JSON confirmation certificate into a Certificate.

    The relay encodes the certificate's three parts INCONSISTENTLY, and
    mixing them up produces failures that look like signature corruption
    rather than a decoding bug:

    - ``signers``: a JSON array of integers (committee POSITIONS)
    - ``serialized_message``: a plain array of integers, NOT base64
    - ``signature``: a standard padded base64 string

    ``signers_bitmap`` is DERIVED here, never taken from the relay --
    exactly as :func:`~pytusk.core.certification.build_certificate` derives
    it on the native path. There is one bitmap encoding in this codebase and
    one place that produces it.

    ``weight`` is likewise recomputed locally: the relay's certificate
    carries no weight information at all. It is the sum of each signer's
    SHARD COUNT, matching ``weight=len(member.shard_indices)`` where the
    native path builds its per-node confirmations. A node holding several
    shards contributes once, because positions are deduplicated first.

    The committee is required rather than a bare size: it supplies both the
    bitmap length (``committee_size``, never ``n_shards``) and the shard
    counts that ``weight`` sums. It must be the committee for the epoch the
    relay certified against -- a certificate is only valid against that
    epoch's signer ordering.

    Args:
        payload (dict): The relay's ``confirmation_certificate`` object,
            already decoded from JSON.
        committee (WalrusCommittee): Committee for the certifying epoch.

    Returns:
        Certificate: Ready for ``certify_blob``, with the same field
            contract as one built from storage-node confirmations.

    Raises:
        RelayCertificateParseError: If the payload is not a dict, is missing
            a field, or any field does not match its documented encoding.
    """
    if not isinstance(payload, dict):
        raise RelayCertificateParseError(
            message=f"Relay certificate must be an object, got {type(payload).__name__}",
            stage=_STAGE,
        )

    try:
        raw_signers = payload["signers"]
        raw_message = payload["serialized_message"]
        raw_signature = payload["signature"]
    except KeyError as exc:
        raise RelayCertificateParseError(
            message=f"Relay certificate is missing field {exc}",
            stage=_STAGE,
        ) from exc

    if not isinstance(raw_signers, list) or not all(
        isinstance(entry, int) and not isinstance(entry, bool) for entry in raw_signers
    ):
        raise RelayCertificateParseError(
            message=f"Relay certificate 'signers' must be a list of integers, got {raw_signers!r}",
            stage=_STAGE,
        )
    if not raw_signers:
        raise RelayCertificateParseError(
            message="Relay certificate 'signers' is empty; nothing signed it",
            stage=_STAGE,
        )

    if not isinstance(raw_message, list):
        raise RelayCertificateParseError(
            message=(
                "Relay certificate 'serialized_message' must be a list of byte "
                f"values, not base64, got {type(raw_message).__name__}"
            ),
            stage=_STAGE,
        )
    try:
        serialized_message = bytes(raw_message)
    except (TypeError, ValueError) as exc:
        raise RelayCertificateParseError(
            message=f"Relay certificate 'serialized_message' is not a byte sequence: {exc}",
            stage=_STAGE,
        ) from exc

    # A valid signature proves the committee signed THESE BYTES. It does not
    # prove the bytes confirm THIS blob: a relay could return a genuine,
    # correctly-signed certificate for some other blob it certified, which
    # would verify locally and then abort inside Move with gas already spent.
    #
    # The epoch is read back OUT of the signed message rather than compared
    # against this committee's. Storage nodes sign their own current epoch at
    # signing time, and the relay aggregates against its own committee view,
    # so pinning our snapshot's epoch here would reject a legitimate
    # certificate AFTER the tip is spent. Identity is what is checked here;
    # epoch correctness stays with signature verification locally and
    # verify_quorum_in_epoch on chain.
    if len(serialized_message) < _EPOCH_OFFSET + 4:
        raise RelayCertificateParseError(
            message=(
                "Relay certificate 'serialized_message' is too short to be a "
                f"confirmation ({len(serialized_message)} bytes)"
            ),
            stage=_STAGE,
        )
    signed_epoch = int.from_bytes(
        serialized_message[_EPOCH_OFFSET : _EPOCH_OFFSET + 4], "little"
    )
    if serialized_message != confirmation_message(
        epoch=signed_epoch, blob_id=blob_id, object_id=object_id
    ):
        raise RelayCertificateParseError(
            message=(
                "Relay certificate does not confirm the blob that was "
                "uploaded: its signed message does not match the expected "
                "confirmation for this blob id and persistence type"
            ),
            stage=_STAGE,
        )

    if not isinstance(raw_signature, str):
        raise RelayCertificateParseError(
            message=(
                "Relay certificate 'signature' must be a base64 string, got "
                f"{type(raw_signature).__name__}"
            ),
            stage=_STAGE,
        )
    try:
        aggregate_signature = decode_standard_base64(value=raw_signature)
    except (binascii.Error, ValueError) as exc:
        raise RelayCertificateParseError(
            message=f"Relay certificate 'signature' is not valid base64: {exc}",
            stage=_STAGE,
        ) from exc

    signer_positions = tuple(sorted(set(raw_signers)))
    try:
        signers_bitmap = pack_signers_bitmap(
            signer_positions=signer_positions,
            committee_size=committee.committee_size,
        )
    except ValueError as exc:
        raise RelayCertificateParseError(
            message=f"Relay certificate names an invalid signer position: {exc}",
            stage=_STAGE,
        ) from exc

    weight = sum(
        len(committee.members[position].shard_indices)
        for position in signer_positions
    )

    # Quorum is enforced HERE, matching build_certificate on the native
    # path. verify_certificate proves only that the aggregate signature is
    # valid for the signers named -- a single honest signer passes it. Without
    # this check a sub-quorum certificate reaches Tx2 and aborts inside
    # verify_quorum_in_epoch, burning gas on something that costs nothing to
    # reject locally.
    if not is_quorum(weight=weight, n_shards=committee.n_shards):
        raise RelayCertificateParseError(
            message=(
                f"Relay certificate carries {weight} of {committee.n_shards} "
                "shards, below the quorum required to certify"
            ),
            stage=_STAGE,
        )

    return Certificate(
        serialized_message=serialized_message,
        aggregate_signature=aggregate_signature,
        signers_bitmap=signers_bitmap,
        signer_positions=signer_positions,
        weight=weight,
    )
