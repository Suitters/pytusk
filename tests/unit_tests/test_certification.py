#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``certify_blob`` wire-format concerns: signer bitmap
packing, quorum arithmetic, storage-confirmation verification, and BLS
aggregation."""

import dataclasses
import math

import pytest
from pysui_fastcrypto import bls_keygen, bls_sign

import pytusk.core.committee as committee_module
from pytusk.core.certification import (
    Certificate,
    ConfirmationMismatchError,
    InvalidConfirmationError,
    NodeConfirmation,
    QuorumNotReachedError,
    build_certificate,
    confirmation_message,
    is_quorum,
    min_weight_for_quorum,
    pack_signers_bitmap,
    unpack_signers_bitmap,
    verify_certificate,
    verify_confirmation,
)
from pytusk.core.encoding import min_n_correct

_EPOCH = 5
_BLOB_ID = bytes(range(32))
_OBJECT_ID = bytes(range(32, 64))


def _make_committee(
    *, weights: list[int], epoch: int = _EPOCH, blob_id: bytes = _BLOB_ID
) -> tuple[bytes, list[NodeConfirmation]]:
    """Mint a synthetic committee of ``len(weights)`` real BLS keypairs.

    Each returned confirmation carries a genuine signature over a real
    ``confirmation_message(...)``, produced by the extension's test-only
    ``bls_keygen``/``bls_sign`` helpers. ``weights[i]`` becomes the shard
    weight of the node at committee position ``i``.

    Args:
        weights (list[int]): Per-position shard weight.
        epoch (int): Walrus epoch for the confirmation message.
        blob_id (bytes): Raw 32-byte blob ID for the confirmation message.

    Returns:
        tuple[bytes, list[NodeConfirmation]]: The agreed message, and one
        valid confirmation per position.
    """
    message = confirmation_message(epoch=epoch, blob_id=blob_id)
    confirmations = []
    for position, weight in enumerate(weights):
        public_key, private_key = bls_keygen()
        signature = bls_sign(private_key, message)
        confirmations.append(
            NodeConfirmation(
                node_id=f"node-{position}",
                position=position,
                weight=weight,
                public_key=public_key,
                serialized_message=message,
                signature=signature,
            )
        )
    return message, confirmations


def _replace(confirmation: NodeConfirmation, **changes: object) -> NodeConfirmation:
    """Return a copy of ``confirmation`` with the given fields replaced.

    ``NodeConfirmation`` is frozen, so tests that need to tamper with one
    field of an otherwise-valid confirmation go through this helper rather
    than constructing an entire replacement by hand at each call site.

    Args:
        confirmation (NodeConfirmation): The confirmation to copy.
        **changes (object): Field values to override.

    Returns:
        NodeConfirmation: The modified copy.
    """
    return dataclasses.replace(confirmation, **changes)


class TestSignersBitmapRoundTrip:
    """Bitmap packing/unpacking round trip and wire-format details."""

    @pytest.mark.parametrize("committee_size", [1, 7, 8, 9, 101, 1000])
    def test_round_trip_all_positions_set(self, committee_size: int) -> None:
        """Every position 0..committee_size-1 round-trips through pack/unpack."""
        positions = tuple(range(committee_size))
        bitmap = pack_signers_bitmap(
            signer_positions=positions, committee_size=committee_size
        )
        assert len(bitmap) == math.ceil(committee_size / 8)
        assert unpack_signers_bitmap(bitmap=bitmap, committee_size=committee_size) == (
            positions
        )

    @pytest.mark.parametrize("committee_size", [1, 7, 8, 9, 101, 1000])
    def test_round_trip_sparse_positions(self, committee_size: int) -> None:
        """A sparse subset of positions round-trips exactly."""
        positions = tuple(p for p in range(committee_size) if p % 3 == 0)
        bitmap = pack_signers_bitmap(
            signer_positions=positions, committee_size=committee_size
        )
        assert len(bitmap) == math.ceil(committee_size / 8)
        assert unpack_signers_bitmap(bitmap=bitmap, committee_size=committee_size) == (
            positions
        )

    def test_byte_length_is_ceil_size_over_8(self) -> None:
        """Bitmap length is ceil(committee_size / 8) for representative sizes."""
        for committee_size, expected_length in (
            (1, 1),
            (7, 1),
            (8, 1),
            (9, 2),
            (101, 13),
            (1000, 125),
        ):
            bitmap = pack_signers_bitmap(
                signer_positions=(), committee_size=committee_size
            )
            assert len(bitmap) == expected_length == math.ceil(committee_size / 8)

    def test_lsb_first_ordering_position_0(self) -> None:
        """Position 0 sets bit 0 of byte 0."""
        bitmap = pack_signers_bitmap(signer_positions=(0,), committee_size=16)
        assert bitmap[0] == 0b0000_0001
        assert bitmap[1] == 0

    def test_lsb_first_ordering_position_8(self) -> None:
        """Position 8 sets bit 0 of byte 1, not bit 8 of byte 0."""
        bitmap = pack_signers_bitmap(signer_positions=(8,), committee_size=16)
        assert bitmap[0] == 0
        assert bitmap[1] == 0b0000_0001

    def test_lsb_first_ordering_position_7(self) -> None:
        """Position 7 sets the high bit of byte 0 (bit 7, not bit 0)."""
        bitmap = pack_signers_bitmap(signer_positions=(7,), committee_size=16)
        assert bitmap[0] == 0b1000_0000
        assert bitmap[1] == 0

    def test_out_of_range_position_raises(self) -> None:
        """A position at or beyond committee_size raises ValueError."""
        with pytest.raises(ValueError):
            pack_signers_bitmap(signer_positions=(8,), committee_size=8)

    def test_negative_position_raises(self) -> None:
        """A negative position raises ValueError."""
        with pytest.raises(ValueError):
            pack_signers_bitmap(signer_positions=(-1,), committee_size=8)


class TestBitmapReExport:
    """committee.py re-exports the bitmap functions from certification.py."""

    def test_pack_is_the_same_object(self) -> None:
        """pack_signers_bitmap is identical across both import paths."""
        assert committee_module.pack_signers_bitmap is pack_signers_bitmap

    def test_unpack_is_the_same_object(self) -> None:
        """unpack_signers_bitmap is identical across both import paths."""
        assert committee_module.unpack_signers_bitmap is unpack_signers_bitmap


class TestQuorum:
    """Quorum weight threshold and minimum-weight arithmetic."""

    @pytest.mark.parametrize(
        ("n_shards", "min_weight"),
        [
            (1, 1),
            (4, 3),
            (6, 5),
            (10, 7),
            (100, 67),
            (1000, 667),
        ],
    )
    def test_min_weight_for_quorum_table(self, n_shards: int, min_weight: int) -> None:
        """min_weight_for_quorum matches the hand-computed ceiling table."""
        assert min_weight_for_quorum(n_shards=n_shards) == min_weight

    def test_min_weight_for_quorum_1000_is_667(self) -> None:
        """Explicit call-out: n_shards=1000 requires weight 667."""
        assert min_weight_for_quorum(n_shards=1000) == 667

    @pytest.mark.parametrize("n_shards", [1, 4, 6, 10, 100, 1000])
    def test_is_quorum_boundary(self, n_shards: int) -> None:
        """One below the minimum weight fails; the minimum itself passes."""
        required = min_weight_for_quorum(n_shards=n_shards)
        assert is_quorum(weight=required, n_shards=n_shards) is True
        assert is_quorum(weight=required - 1, n_shards=n_shards) is False

    @pytest.mark.parametrize("n_shards", list(range(1, 200)))
    def test_min_weight_for_quorum_matches_min_n_correct(self, n_shards: int) -> None:
        """min_weight_for_quorum coincides with min_n_correct for every tested
        shard count -- guards against future divergence between the quorum
        formula and the Byzantine fault-tolerance formula."""
        assert min_weight_for_quorum(n_shards=n_shards) == min_n_correct(
            n_shards=n_shards
        )


class TestConfirmationMessage:
    """Confirmation message byte lengths for permanent vs. deletable blobs."""

    def test_permanent_blob_is_40_bytes(self) -> None:
        """Omitting object_id yields the 40-byte permanent form."""
        message = confirmation_message(epoch=_EPOCH, blob_id=_BLOB_ID)
        assert len(message) == 40

    def test_deletable_blob_is_72_bytes(self) -> None:
        """Supplying a 32-byte object_id yields the 72-byte deletable form."""
        message = confirmation_message(
            epoch=_EPOCH, blob_id=_BLOB_ID, object_id=_OBJECT_ID
        )
        assert len(message) == 72


class TestVerifyConfirmation:
    """Single-confirmation verification."""

    def test_valid_confirmation_verifies(self) -> None:
        """A genuine signature over the expected message verifies True."""
        message, confirmations = _make_committee(weights=[1])
        assert (
            verify_confirmation(confirmation=confirmations[0], expected_message=message)
            is True
        )

    def test_message_mismatch_returns_false(self) -> None:
        """A different expected_message returns False without raising."""
        message, confirmations = _make_committee(weights=[1])
        other_message = confirmation_message(epoch=_EPOCH + 1, blob_id=_BLOB_ID)
        assert other_message != message
        assert (
            verify_confirmation(
                confirmation=confirmations[0], expected_message=other_message
            )
            is False
        )

    def test_unparseable_signature_returns_false_not_raises(self) -> None:
        """A signature that cannot be parsed as a G2 point returns False --
        the extension's raises-ValueError path is normalised to a plain
        boolean, since callers are a predicate over untrusted node input."""
        message, confirmations = _make_committee(weights=[1])
        malformed = _replace(confirmations[0], signature=b"\xff" * 96)
        assert (
            verify_confirmation(confirmation=malformed, expected_message=message)
            is False
        )

    def test_malformed_public_key_returns_false_not_raises(self) -> None:
        """A public key that cannot be parsed returns False rather than
        propagating the extension's ValueError."""
        message, confirmations = _make_committee(weights=[1])
        malformed = _replace(confirmations[0], public_key=b"\x00" * 10)
        assert (
            verify_confirmation(confirmation=malformed, expected_message=message)
            is False
        )


class TestBuildCertificateHappyPath:
    """Successful certificate construction and local verification."""

    def test_quorum_signers_build_and_verify(self) -> None:
        """A committee where signers meet quorum builds a verifying certificate."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        message, confirmations = _make_committee(weights=weights, blob_id=_BLOB_ID)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = confirmations[:required]

        certificate = build_certificate(
            confirmations=signers, committee_size=committee_size, n_shards=n_shards
        )

        assert isinstance(certificate, Certificate)
        assert certificate.serialized_message == message
        assert certificate.weight == required
        assert certificate.signer_positions == tuple(range(required))
        assert (
            verify_certificate(
                certificate=certificate,
                public_keys=[c.public_key for c in signers],
            )
            is True
        )

    def test_signer_positions_sorted_even_when_supplied_out_of_order(self) -> None:
        """signer_positions is ascending regardless of confirmation order."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(reversed(confirmations[:required]))
        assert [c.position for c in signers] != sorted(c.position for c in signers)

        certificate = build_certificate(
            confirmations=signers, committee_size=committee_size, n_shards=n_shards
        )

        assert certificate.signer_positions == tuple(sorted(range(required)))


class TestBuildCertificateErrors:
    """Failure paths for certificate construction."""

    def test_empty_confirmations_raises(self) -> None:
        """An empty confirmation sequence raises ValueError."""
        with pytest.raises(ValueError):
            build_certificate(confirmations=[], committee_size=10, n_shards=10)

    def test_message_mismatch_raises_confirmation_mismatch(self) -> None:
        """A node returning a different serialized_message raises."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        other_message = confirmation_message(epoch=_EPOCH + 1, blob_id=_BLOB_ID)
        tampered = _replace(signers[0], serialized_message=other_message)
        signers[0] = tampered

        with pytest.raises(ConfirmationMismatchError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_unparseable_signature_raises_invalid_confirmation(self) -> None:
        """A signature that is not a valid G2 point encoding cannot even be
        parsed -- this exercises the extension's RAISES-ValueError path (as
        opposed to its returns-False path), and must still surface as
        InvalidConfirmationError, not a raw ValueError, from build_certificate."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        # 96 0xFF bytes is the right length for an uncompressed G2 point but
        # is not a valid point encoding -- bls_verify raises ValueError for
        # this input rather than returning False.
        signers[0] = _replace(signers[0], signature=b"\xff" * 96)

        with pytest.raises(InvalidConfirmationError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_flipped_byte_signature_raises_invalid_confirmation(self) -> None:
        """A signature with one flipped byte is also expected to become an
        unparseable point encoding, exercising the same RAISES-ValueError
        path as the all-0xFF case via a more realistic corruption."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        corrupted_signature = bytearray(signers[0].signature)
        corrupted_signature[0] ^= 0xFF
        signers[0] = _replace(signers[0], signature=bytes(corrupted_signature))

        with pytest.raises(InvalidConfirmationError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_wellformed_wrong_signature_raises_invalid_confirmation(self) -> None:
        """A signature that is a genuine BLS signature -- just over the wrong
        message -- parses correctly but fails verification. This exercises
        the extension's RETURNS-False path (as opposed to its
        raises-ValueError path), and must still surface as
        InvalidConfirmationError from build_certificate."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        message, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        # Re-sign the SAME key over a DIFFERENT message: a well-formed,
        # parseable signature that simply does not verify against `message`.
        other_message = confirmation_message(epoch=_EPOCH + 1, blob_id=_BLOB_ID)
        assert other_message != message
        public_key, private_key = bls_keygen()
        wrong_signature = bls_sign(private_key, other_message)
        signers[0] = _replace(
            signers[0], public_key=public_key, signature=wrong_signature
        )

        with pytest.raises(InvalidConfirmationError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_signature_from_wrong_key_raises_invalid_confirmation(self) -> None:
        """A valid signature paired with the wrong declared public_key fails."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        wrong_public_key, _ = bls_keygen()
        signers[0] = _replace(signers[0], public_key=wrong_public_key)

        with pytest.raises(InvalidConfirmationError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_malformed_public_key_raises_invalid_confirmation(self) -> None:
        """A public key that is not a valid G1/G2 point encoding cannot even
        be parsed -- bls_verify raises ValueError for this input, and it must
        surface as InvalidConfirmationError, not a raw ValueError, from
        build_certificate."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        # Wrong length: neither the 48-byte compressed nor 96-byte
        # uncompressed encoding accepted for a committee public key.
        signers[0] = _replace(signers[0], public_key=b"\x00" * 10)

        with pytest.raises(InvalidConfirmationError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_aggregate_first_still_names_the_specific_bad_signer(self) -> None:
        """The aggregate-first restructure must still identify WHICH signer
        is bad on failure, not merely that the aggregate failed. Corrupts a
        signer in the MIDDLE of a larger set (not position 0) so a
        fallback loop that stopped at the first confirmation, or an error
        that only ever names the first confirmation, would fail this test."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        assert required > 3, "test assumes at least 4 signers to corrupt the middle one"
        bad_index = 3
        bad_node_id = signers[bad_index].node_id
        other_node_ids = [c.node_id for i, c in enumerate(signers) if i != bad_index]
        wrong_public_key, _ = bls_keygen()
        signers[bad_index] = _replace(signers[bad_index], public_key=wrong_public_key)

        with pytest.raises(InvalidConfirmationError) as exc_info:
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

        message = str(exc_info.value)
        assert bad_node_id in message
        for other_node_id in other_node_ids:
            assert other_node_id not in message

    def test_duplicate_position_raises_value_error(self) -> None:
        """Two confirmations at the same committee position raise ValueError."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        signers.append(_replace(signers[0]))

        with pytest.raises(ValueError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_one_short_of_quorum_raises(self) -> None:
        """Total weight one below the minimum raises QuorumNotReachedError."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = confirmations[: required - 1]

        with pytest.raises(QuorumNotReachedError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_exactly_at_quorum_succeeds(self) -> None:
        """Total weight exactly at the minimum succeeds (boundary, success side)."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = confirmations[:required]

        certificate = build_certificate(
            confirmations=signers, committee_size=committee_size, n_shards=n_shards
        )
        assert certificate.weight == required


class TestVerifyCertificate:
    """Local certificate verification, including malformed-input handling."""

    def test_malformed_aggregate_signature_returns_false_not_raises(self) -> None:
        """An aggregate signature that cannot be parsed as a G2 point returns
        False -- the extension's raises-ValueError path is normalised to a
        plain boolean, since this function is a local predicate, not a
        validator, and must not leak a raw ValueError."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = confirmations[:required]

        certificate = build_certificate(
            confirmations=signers, committee_size=committee_size, n_shards=n_shards
        )
        malformed_certificate = dataclasses.replace(
            certificate, aggregate_signature=b"\xff" * 96
        )

        assert (
            verify_certificate(
                certificate=malformed_certificate,
                public_keys=[c.public_key for c in signers],
            )
            is False
        )
