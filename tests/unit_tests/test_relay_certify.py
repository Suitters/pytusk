#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for relay confirmation-certificate parsing."""

import base64

import pytest

from pytusk.core.certification import unpack_signers_bitmap
from pytusk.core.committee import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.relay_upload.common import RelayCertificateParseError
from pytusk.core.relay_upload.relay_certify import parse_relay_certificate

_SIGNATURE_BYTES = b"aggregate-signature-bytes"
_SIGNATURE_B64 = base64.b64encode(_SIGNATURE_BYTES).decode("ascii")


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
        epoch=7, n_shards=sum(shard_counts), members=tuple(members)
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
            [1, 2, 3] if serialized_message is None else serialized_message
        ),
        "signature": _SIGNATURE_B64 if signature is None else signature,
    }


class TestParseRelayCertificate:
    """The three inconsistently-encoded fields, decoded correctly."""

    def test_decodes_each_field_by_its_own_encoding(self) -> None:
        cert = parse_relay_certificate(
            payload=_payload(), committee=_committee()
        )
        assert cert.serialized_message == bytes([1, 2, 3])
        assert cert.aggregate_signature == _SIGNATURE_BYTES
        assert cert.signer_positions == (0, 2)

    def test_serialized_message_is_not_base64_decoded(self) -> None:
        cert = parse_relay_certificate(
            payload=_payload(serialized_message=[65, 81, 73, 68]),
            committee=_committee(),
        )
        assert cert.serialized_message == bytes([65, 81, 73, 68])

    def test_bitmap_is_derived_not_taken(self) -> None:
        committee = _committee()
        cert = parse_relay_certificate(payload=_payload(), committee=committee)
        assert unpack_signers_bitmap(
            bitmap=cert.signers_bitmap, committee_size=committee.committee_size
        ) == (0, 2)

    def test_bitmap_length_follows_committee_size_not_n_shards(self) -> None:
        committee = _committee()
        cert = parse_relay_certificate(payload=_payload(), committee=committee)
        assert len(cert.signers_bitmap) == (committee.committee_size + 7) // 8
        assert committee.n_shards != committee.committee_size

    def test_weight_sums_signer_shard_counts(self) -> None:
        cert = parse_relay_certificate(
            payload=_payload(signers=[0, 2]), committee=_committee()
        )
        assert cert.weight == 5

    def test_positions_are_sorted_and_deduplicated(self) -> None:
        cert = parse_relay_certificate(
            payload=_payload(signers=[2, 0, 2]), committee=_committee()
        )
        assert cert.signer_positions == (0, 2)
        assert cert.weight == 5


class TestParseRelayCertificateRejects:
    """Malformed payloads are terminal, not transient."""

    def test_non_dict_payload(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="must be an object"):
            parse_relay_certificate(payload="nope", committee=_committee())

    def test_missing_field(self) -> None:
        payload = _payload()
        del payload["signature"]
        with pytest.raises(RelayCertificateParseError, match="missing field"):
            parse_relay_certificate(payload=payload, committee=_committee())

    def test_signers_not_a_list(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="must be a list"):
            parse_relay_certificate(
                payload=_payload(signers="0,2"), committee=_committee()
            )

    def test_signers_not_integers(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="must be a list"):
            parse_relay_certificate(
                payload=_payload(signers=[0, "2"]), committee=_committee()
            )

    def test_signers_empty(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="nothing signed it"):
            parse_relay_certificate(
                payload=_payload(signers=[]), committee=_committee()
            )

    def test_serialized_message_as_base64_string_is_rejected(self) -> None:
        with pytest.raises(RelayCertificateParseError, match="not base64"):
            parse_relay_certificate(
                payload=_payload(serialized_message="AQID"), committee=_committee()
            )

    def test_serialized_message_out_of_byte_range(self) -> None:
        with pytest.raises(
            RelayCertificateParseError, match="not a byte sequence"
        ):
            parse_relay_certificate(
                payload=_payload(serialized_message=[1, 256]),
                committee=_committee(),
            )

    def test_signature_not_a_string(self) -> None:
        with pytest.raises(
            RelayCertificateParseError, match="must be a base64 string"
        ):
            parse_relay_certificate(
                payload=_payload(signature=[1, 2, 3]), committee=_committee()
            )

    def test_signer_position_outside_committee(self) -> None:
        with pytest.raises(
            RelayCertificateParseError, match="invalid signer position"
        ):
            parse_relay_certificate(
                payload=_payload(signers=[0, 99]), committee=_committee()
            )
