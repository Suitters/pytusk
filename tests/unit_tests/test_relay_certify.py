#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for relay confirmation-certificate parsing."""

import base64

import pytest

from pytusk.core.certification import (
    Certificate,
    confirmation_message,
    unpack_signers_bitmap,
)
from pytusk.core.chain import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.relay_upload.relay_certify import parse_relay_certificate
from pytusk.core.types import RelayCertificateParseError

_SIGNATURE_BYTES = b"aggregate-signature-bytes"
_SIGNATURE_B64 = base64.b64encode(_SIGNATURE_BYTES).decode("ascii")

_BLOB_ID: bytes = bytes(range(32))
"""Raw 32-byte blob id every fixture confirmation is built for."""

_OBJECT_ID: bytes = bytes([0xAB]) * 32
"""Raw 32-byte blob object id, for the deletable-persistence fixtures."""

_COMMITTEE_EPOCH: int = 7
"""Epoch of the fixture committee, and of a confirmation unless overridden."""


def _confirmation(
    *,
    epoch: int = _COMMITTEE_EPOCH,
    blob_id: bytes = _BLOB_ID,
    object_id: bytes | None = None,
) -> bytes:
    """Build the exact bytes a storage node signs to confirm a blob.

    Fixtures cannot use arbitrary byte arrays: the parser rebuilds the
    confirmation itself and rejects anything that is not the message for
    the blob id and persistence type it was handed.
    """
    return confirmation_message(epoch=epoch, blob_id=blob_id, object_id=object_id)


def _committee(*, shard_counts: tuple[int, ...] = (3, 1, 2, 1)) -> WalrusCommittee:
    """Build a committee whose members hold the given shard counts."""
    members = []
    next_shard = 0
    for index, count in enumerate(shard_counts):
        members.append(
            WalrusCommitteeMember(
                node_id=f"0xnode{index}",
                shard_indices=tuple(range(next_shard, next_shard + count)),
                network_address=f"node{index}.example.com:9185",
                public_key=bytes([index]) * 48,
            )
        )
        next_shard += count
    return WalrusCommittee(
        epoch=_COMMITTEE_EPOCH, n_shards=sum(shard_counts), members=tuple(members)
    )


def _payload(
    *,
    signers: object = None,
    serialized_message: object = None,
    signature: object = None,
) -> dict:
    """Build a relay certificate payload, overriding individual fields."""
    return {
        "signers": [0, 2] if signers is None else signers,
        "serialized_message": (
            list(_confirmation())
            if serialized_message is None
            else serialized_message
        ),
        "signature": _SIGNATURE_B64 if signature is None else signature,
    }


def _parse(
    *,
    payload: object,
    committee: WalrusCommittee | None = None,
    blob_id: bytes = _BLOB_ID,
    object_id: bytes | None = None,
) -> Certificate:
    """Call the parser, defaulting the committee and identity arguments."""
    return parse_relay_certificate(
        payload=payload,
        committee=_committee() if committee is None else committee,
        blob_id=blob_id,
        object_id=object_id,
    )


class TestParseRelayCertificate:
    """The three inconsistently-encoded fields, decoded correctly."""

    def test_decodes_each_field_by_its_own_encoding(self) -> None:
        cert = _parse(payload=_payload())
        assert cert.serialized_message == _confirmation()
        assert cert.aggregate_signature == _SIGNATURE_BYTES
        assert cert.signer_positions == (0, 2)

    def test_bitmap_is_derived_not_taken(self) -> None:
        committee = _committee()
        cert = _parse(payload=_payload(), committee=committee)
        assert unpack_signers_bitmap(
            bitmap=cert.signers_bitmap, committee_size=committee.committee_size
        ) == (0, 2)

    def test_bitmap_length_follows_committee_size_not_n_shards(self) -> None:
        committee = _committee()
        cert = _parse(payload=_payload(), committee=committee)
        assert len(cert.signers_bitmap) == (committee.committee_size + 7) // 8
        assert committee.n_shards != committee.committee_size

    def test_weight_sums_signer_shard_counts(self) -> None:
        cert = _parse(payload=_payload(signers=[0, 2]))
        assert cert.weight == 5

    def test_positions_are_sorted_and_deduplicated(self) -> None:
        cert = _parse(payload=_payload(signers=[2, 0, 2]))
        assert cert.signer_positions == (0, 2)
        assert cert.weight == 5

    def test_message_epoch_need_not_match_the_committee_snapshot(self) -> None:
        """A node signs its OWN current epoch, which may lead our snapshot.

        The parser reads the epoch back OUT of the signed message rather
        than comparing it to the committee it was handed. Pinning our
        snapshot here would reject a legitimate certificate AFTER the tip
        is spent.
        """
        committee = _committee()
        ahead = _confirmation(epoch=committee.epoch + 2)
        cert = _parse(
            payload=_payload(serialized_message=list(ahead)), committee=committee
        )
        assert cert.serialized_message == ahead


class TestParseRelayCertificateRejects:
    """Malformed payloads are terminal, not transient."""

    def test_non_dict_payload(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="must be an object"):
            _parse(payload="nope")

    def test_missing_field(self) -> None:
        payload = _payload()
        del payload["signature"]
        with pytest.raises(RelayCertificateParseError, match="missing field"):
            _parse(payload=payload)

    def test_signers_not_a_list(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="must be a list"):
            _parse(payload=_payload(signers="0,2"))

    def test_signers_not_integers(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="must be a list"):
            _parse(payload=_payload(signers=[0, "2"]))

    def test_signers_empty(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="nothing signed it"):
            _parse(payload=_payload(signers=[]))

    def test_serialized_message_as_base64_string_is_rejected(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="not base64"):
            _parse(payload=_payload(serialized_message="AQID"))

    def test_serialized_message_out_of_byte_range(self) -> None:
        with pytest.raises(
            RelayCertificateParseError, match="not a byte sequence"
        ):
            _parse(payload=_payload(serialized_message=[1, 256]))

    def test_signature_not_a_string(self) -> None:
        with pytest.raises(
            RelayCertificateParseError, match="must be a base64 string"
        ):
            _parse(payload=_payload(signature=[1, 2, 3]))

    def test_signer_position_outside_committee(self) -> None:
        with pytest.raises(
            RelayCertificateParseError, match="invalid signer position"
        ):
            _parse(payload=_payload(signers=[0, 99]))


class TestParseRelayCertificateIdentity:
    """A well-formed certificate must confirm THIS blob, not merely verify.

    A relay could return a genuine, correctly-signed certificate for some
    other blob it certified. It would verify locally and then abort inside
    Move with gas already spent.
    """

    def test_confirmation_for_another_blob_is_rejected(self) -> None:
        other = bytes([0xFF]) * 32
        with pytest.raises(
            RelayCertificateParseError, match="does not confirm the blob"
        ):
            _parse(
                payload=_payload(
                    serialized_message=list(_confirmation(blob_id=other))
                )
            )

    def test_deletable_confirmation_is_rejected_for_a_permanent_blob(self) -> None:
        with pytest.raises(
            RelayCertificateParseError, match="does not confirm the blob"
        ):
            _parse(
                payload=_payload(
                    serialized_message=list(_confirmation(object_id=_OBJECT_ID))
                ),
                object_id=None,
            )

    def test_message_too_short_to_hold_an_epoch(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="too short"):
            _parse(payload=_payload(serialized_message=[1, 2, 3]))


class TestParseRelayCertificateQuorum:
    """A sub-quorum certificate is refused locally, not on chain.

    Signature verification proves only that the named signers signed: a
    single honest signer passes it. Without this the certificate reaches
    Tx2 and aborts inside ``verify_quorum_in_epoch``, burning gas on
    something that costs nothing to reject here.
    """

    def test_below_quorum_is_rejected(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="below the quorum"):
            _parse(payload=_payload(signers=[1]))
