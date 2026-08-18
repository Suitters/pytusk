#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``certify_blob`` wire-format concerns: signer bitmap
packing, quorum arithmetic, storage-confirmation verification, and BLS
aggregation."""

import dataclasses
import math
from unittest.mock import patch

import pytest

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

# Fixed BLS12-381 vector: three keypairs signing the same message, generated
# once via `fastcrypto` directly (the same way `bls.rs`'s own Rust tests do)
# and hardcoded here. This crate exposes no signing function to Python, so
# these bytes are the only way the tests below can exercise a genuine
# verify/aggregate round trip through the FFI boundary. Frozen data, not a
# protocol vector -- it proves the FFI plumbing calls into `fastcrypto`
# correctly, not anything about Walrus wire-format correctness.
#
# To regenerate: `cargo test --lib print_python_test_vector -- --ignored --nocapture`
# (src/walrus/bls.rs), then paste the printed hex below.
_BLS_MESSAGE = b"walrus storage confirmation"
_BLS_PUBLIC_KEYS = [
    bytes.fromhex(
        "86fa236e1d74d7f4e0505833258d9cf8109d8c6d0d3f7fdeb5f07124771071ebce15dd95fd9945e421be2263d277f7c4"
    ),
    bytes.fromhex(
        "ac91600470572da456a0c73ae1693cc3ee1add243fcaa19553f7efc42d5ede059afd3b6fde4da2fc7fd0b2829ac5456b"
    ),
    bytes.fromhex(
        "8ea3b0269e27b3da4fdeedcfa4c6347d95d463c6b1f4a769babb84d61e5f60f7bbad98c372c20e1556190cc03938530c"
    ),
]
_BLS_SIGNATURES = [
    bytes.fromhex(
        "a07435357105bd9eb10ff17eab5913362cd1fba0b7276d8fbe13cc6503e237ec525c6d002fdd6d554900426c82684e7c049987bb406dd552d7cead8b0222c06be9b2f4096406f9c49967d596ec8b8b17a0d1372d6246725fcd7fc2d6f04d7d61"
    ),
    bytes.fromhex(
        "8f0b76e592185600ebb090a94f90e29a1f4587a7ea1e409037c66b1c3a6dae7c65597c01e587f77cde06b190235faf6c177f5f93c2ebb5c9d5ef33cf94398219b2954b02d82b6b80ba3730a705175977df727dce5da121fe062cc1c62c8877c9"
    ),
    bytes.fromhex(
        "8ad418d76193ea319773ab6982d60792598bf6b300c99510ae755462aa9bed15388182423dee9ed6bd54fcb7a4206ec20ee0b7c46f2e367691442a28e01cfa036dfd59de571173da9c30004e62d970b2b171527dfb5df1e28fc88b48c62d1f3e"
    ),
]


def _make_committee(*, weights: list[int]) -> tuple[bytes, list[NodeConfirmation]]:
    """Mint a synthetic committee of ``len(weights)`` confirmations.

    Each returned confirmation carries a genuine signature over the fixed
    ``_BLS_MESSAGE`` test vector, cycling through the 3 available real
    keypairs by position. ``weights[i]`` becomes the shard weight of the
    node at committee position ``i``.

    Args:
        weights (list[int]): Per-position shard weight.

    Returns:
        tuple[bytes, list[NodeConfirmation]]: The agreed message, and one
        valid confirmation per position.
    """
    confirmations = []
    for position, weight in enumerate(weights):
        index = position % len(_BLS_PUBLIC_KEYS)
        confirmations.append(
            NodeConfirmation(
                node_id=f"node-{position}",
                position=position,
                weight=weight,
                public_key=_BLS_PUBLIC_KEYS[index],
                serialized_message=_BLS_MESSAGE,
                signature=_BLS_SIGNATURES[index],
            )
        )
    return _BLS_MESSAGE, confirmations


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
        """A committee where signers meet quorum builds a verifying certificate.

        Only 3 real keypairs are available (see ``_BLS_PUBLIC_KEYS``), and
        this test needs 7 distinct signers to reach quorum, so
        ``bls_aggregate``/``bls_aggregate_verify`` are mocked here -- this
        test is about quorum/certificate-construction logic, not crypto
        correctness, which the fallback-loop tests below exercise for real.
        """
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        message, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = confirmations[:required]

        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
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

        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
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

    def test_unparseable_signature_excluded_drops_below_quorum(self) -> None:
        """A signature that is not a valid G2 point encoding cannot even be
        parsed -- this exercises the extension's RAISES-ValueError path (as
        opposed to its returns-False path). The bad confirmation is
        EXCLUDED rather than failing immediately; since this fixture is
        exactly at quorum, excluding it drops weight below quorum and
        QuorumNotReachedError is raised."""
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

        with pytest.raises(QuorumNotReachedError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_flipped_byte_signature_excluded_drops_below_quorum(self) -> None:
        """A signature with one flipped byte is also expected to become an
        unparseable point encoding, exercising the same RAISES-ValueError
        path as the all-0xFF case via a more realistic corruption. The bad
        confirmation is EXCLUDED rather than failing immediately; since this
        fixture is exactly at quorum, excluding it drops weight below
        quorum and QuorumNotReachedError is raised."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        corrupted_signature = bytearray(signers[0].signature)
        corrupted_signature[0] ^= 0xFF
        signers[0] = _replace(signers[0], signature=bytes(corrupted_signature))

        with pytest.raises(QuorumNotReachedError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_wellformed_wrong_signature_excluded_drops_below_quorum(self) -> None:
        """A signature that is a genuine BLS signature -- just from a
        DIFFERENT keypair -- parses correctly but fails verification. This
        exercises the extension's RETURNS-False path (as opposed to its
        raises-ValueError path). The bad confirmation is EXCLUDED rather
        than failing immediately; since this fixture is exactly at quorum,
        excluding it drops weight below quorum and QuorumNotReachedError is
        raised."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        # signers[0] keeps its own public key but gets a DIFFERENT real
        # signature: well-formed and parseable, but does not verify against
        # its declared public key.
        signers[0] = _replace(signers[0], signature=_BLS_SIGNATURES[1])

        with pytest.raises(QuorumNotReachedError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_signature_from_wrong_key_excluded_drops_below_quorum(self) -> None:
        """A valid signature paired with the wrong declared public_key fails.
        The bad confirmation is EXCLUDED rather than failing immediately;
        since this fixture is exactly at quorum, excluding it drops weight
        below quorum and QuorumNotReachedError is raised."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        # signers[0] keeps its own signature but declares a DIFFERENT real
        # public key: well-formed, but the pairing does not verify.
        signers[0] = _replace(signers[0], public_key=_BLS_PUBLIC_KEYS[1])

        with pytest.raises(QuorumNotReachedError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_malformed_public_key_excluded_drops_below_quorum(self) -> None:
        """A public key that is not a valid G1/G2 point encoding cannot even
        be parsed -- bls_verify raises ValueError for this input. The bad
        confirmation is EXCLUDED rather than failing immediately; since this
        fixture is exactly at quorum, excluding it drops weight below
        quorum and QuorumNotReachedError is raised."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        # Wrong length: neither the 48-byte compressed nor 96-byte
        # uncompressed encoding accepted for a committee public key.
        signers[0] = _replace(signers[0], public_key=b"\x00" * 10)

        with pytest.raises(QuorumNotReachedError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_aggregate_first_still_names_the_specific_bad_signer(self) -> None:
        """The aggregate-first restructure must still identify WHICH signer
        is bad, not merely that the aggregate failed. Corrupts a signer in
        the MIDDLE of a larger set (not position 0) so a fallback loop that
        stopped at the first confirmation, or an error that only ever names
        the first confirmation, would fail this test. The bad confirmation
        is EXCLUDED rather than failing immediately; since this fixture is
        exactly at quorum, excluding it drops weight below quorum and
        QuorumNotReachedError is raised -- its message still names the
        excluded node."""
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
        # Keeps its own signature but declares a DIFFERENT real public key:
        # well-formed, but the pairing does not verify.
        signers[bad_index] = _replace(
            signers[bad_index], public_key=_BLS_PUBLIC_KEYS[1]
        )

        with pytest.raises(QuorumNotReachedError) as exc_info:
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

        message = str(exc_info.value)
        assert bad_node_id in message
        for other_node_id in other_node_ids:
            assert other_node_id not in message

    def test_bad_signature_excluded_when_quorum_still_reached(self) -> None:
        """A single corrupted signature among a SURPLUS of signers is
        excluded, and quorum is rechecked against the rest -- this is the
        whole point of the exclude-and-recheck fallback: a bad confirmation
        must not abort an otherwise-valid certificate when enough good
        weight remains after excluding it."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        # One extra signer beyond quorum, so excluding the bad one still
        # leaves exactly `required` good weight.
        signers = list(confirmations[: required + 1])
        # signers[0] keeps its own signature but declares a DIFFERENT real
        # public key: well-formed, but the pairing does not verify.
        signers[0] = _replace(signers[0], public_key=_BLS_PUBLIC_KEYS[1])

        certificate = build_certificate(
            confirmations=signers, committee_size=committee_size, n_shards=n_shards
        )

        assert certificate.weight == required
        assert certificate.signer_positions == tuple(range(1, required + 1))

    def test_all_signatures_invalid_raises_invalid_confirmation(self) -> None:
        """When EVERY confirmation fails per-node verification, there is
        nothing left to exclude down to -- this must still raise
        InvalidConfirmationError rather than silently returning an empty or
        unverified certificate, or a misleading QuorumNotReachedError."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = [
            _replace(
                c, public_key=_BLS_PUBLIC_KEYS[(i + 1) % len(_BLS_PUBLIC_KEYS)]
            )
            for i, c in enumerate(confirmations[:required])
        ]

        with pytest.raises(InvalidConfirmationError):
            build_certificate(
                confirmations=signers, committee_size=committee_size, n_shards=n_shards
            )

    def test_duplicate_position_raises_value_error(self) -> None:
        """Two confirmations at the same committee position raise ValueError."""
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = list(confirmations[:required])
        signers.append(_replace(signers[0]))

        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
            pytest.raises(ValueError),
        ):
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

        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
            pytest.raises(QuorumNotReachedError),
        ):
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

        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
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
        validator, and must not leak a raw ValueError.

        The certificate is built via mocked aggregate calls (only 3 real
        keypairs are available and this test needs 7), but the final
        ``verify_certificate`` call below is UNMOCKED -- it must exercise
        the real extension's ValueError-on-malformed-input path.
        """
        committee_size = 10
        n_shards = 10
        weights = [1] * committee_size
        _, confirmations = _make_committee(weights=weights)
        required = min_weight_for_quorum(n_shards=n_shards)
        signers = confirmations[:required]

        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
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
