#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for RedStuff encoding and blob-ID / root-hash conversions."""

import pytest

from pytusk.core.encoding import (
    BlobDecodeError,
    BlobTooLargeError,
    EncodedBlob,
    MetadataVerificationError,
    VerifiedBlobMetadata,
    blob_id_from_url_base64,
    blob_id_to_u256,
    blob_id_to_url_base64,
    decode_blob,
    decode_standard_base64,
    encode_blob,
    encoded_blob_length,
    max_blob_size,
    max_n_faulty,
    metadata_length,
    min_n_correct,
    pair_index_to_shard_index,
    root_hash_to_u256,
    rotation_offset,
    shard_index_to_pair_index,
    source_symbol_counts,
    symbol_size,
    verify_blob_metadata,
)


class TestSourceSymbolCounts:
    """Primary/secondary source symbol counts against the upstream table."""

    @pytest.mark.parametrize(
        ("n_shards", "primary", "secondary"),
        [
            (1, 1, 1),
            (3, 3, 3),
            (7, 3, 5),
            (10, 4, 7),
            (31, 11, 21),
            (100, 34, 67),
            (301, 101, 201),
            (1000, 334, 667),
        ],
    )
    def test_matches_upstream_table(
        self, n_shards: int, primary: int, secondary: int
    ) -> None:
        """(primary, secondary) matches the upstream reference table."""
        assert source_symbol_counts(n_shards=n_shards) == (primary, secondary)

    def test_f_plus_one_approximation_is_wrong_for_non_congruent_shard_count(
        self,
    ) -> None:
        """REGRESSION: n_shards - 2f / n_shards - f is not the same as f+1 / 2f+1.

        For n_shards=6, f=1. The (wrong) f+1/2f+1 approximation would give
        (2, 3). The correct upstream formula gives (4, 5). This only agrees
        with the approximation when n_shards % 3 == 1, which 6 is not.
        """
        primary, secondary = source_symbol_counts(n_shards=6)
        assert (primary, secondary) == (4, 5)
        assert primary != 2
        assert secondary != 3


class TestMaxNFaultyAndMinNCorrect:
    """Byzantine fault tolerance bounds."""

    @pytest.mark.parametrize(
        ("n_shards", "max_faulty", "min_correct"),
        [
            (1, 0, 1),
            (3, 0, 3),
            (4, 1, 3),
            (5, 1, 4),
            (6, 1, 5),
            (100, 33, 67),
            (300, 99, 201),
        ],
    )
    def test_matches_upstream_table(
        self, n_shards: int, max_faulty: int, min_correct: int
    ) -> None:
        """max_n_faulty and min_n_correct match the upstream reference table."""
        assert max_n_faulty(n_shards=n_shards) == max_faulty
        assert min_n_correct(n_shards=n_shards) == min_correct


class TestMaxBlobSize:
    """Maximum encodable blob size, derived from the live shard count."""

    def test_n_shards_1000(self) -> None:
        """At n_shards=1000 the limit is ~13.6 GiB."""
        assert max_blob_size(n_shards=1000) == 14_599_533_452

    def test_n_shards_100(self) -> None:
        """At n_shards=100 the limit is smaller, still shard-count derived."""
        assert max_blob_size(n_shards=100) == 149_286_452


class TestSymbolSize:
    """RS2 symbol size computation."""

    def test_empty_blob_is_positive(self) -> None:
        """An empty blob (length 0) is treated as length 1, never yielding 0."""
        assert symbol_size(blob_length=0, n_shards=7) > 0

    @pytest.mark.parametrize("n_shards", [1, 3, 6, 7, 10, 31, 100])
    def test_result_is_always_even(self, n_shards: int) -> None:
        """The result always rounds up to a multiple of RS2_REQUIRED_ALIGNMENT."""
        for blob_length in (0, 1, 5, 100, 10_000):
            assert symbol_size(blob_length=blob_length, n_shards=n_shards) % 2 == 0

    @pytest.mark.parametrize(
        ("blob_length", "n_shards", "expected"),
        [
            (0, 7, 2),
            (15, 7, 2),
            (16, 7, 2),
            (30, 7, 2),
            (31, 7, 4),
            (100, 10, 4),
            (113, 10, 6),
        ],
    )
    def test_hand_computed_cases(
        self, blob_length: int, n_shards: int, expected: int
    ) -> None:
        """Hand-computed symbol sizes for small blob/shard combinations."""
        assert symbol_size(blob_length=blob_length, n_shards=n_shards) == expected


class TestMetadataLength:
    """Per-shard Red Stuff metadata size, mirroring ``metadata_size`` in
    ``redstuff.move`` (``n_shards * DIGEST_LEN * 2 + BLOB_ID_LEN``, with
    ``DIGEST_LEN = BLOB_ID_LEN = 32``)."""

    @pytest.mark.parametrize(
        ("n_shards", "expected"),
        [
            (10, 10 * 32 * 2 + 32),  # 672, matches redstuff.move's own test table
            (1000, 1000 * 32 * 2 + 32),  # 64_032
        ],
    )
    def test_matches_move_formula(self, n_shards: int, expected: int) -> None:
        """metadata_length(n_shards) == n_shards * 32 * 2 + 32."""
        assert metadata_length(n_shards=n_shards) == expected


class TestEncodedBlobLength:
    """Total on-chain encoded blob size, mirroring ``encoded_blob_length`` in
    ``redstuff.move`` (lines 17-25): ``n_shards * ((primary + secondary) *
    symbol_size + metadata_length)``.

    Cases below are taken directly from -- or derived by hand from the same
    arithmetic as -- ``redstuff.move``'s own ``test_encoded_size_reed_solomon``
    and ``test_zero_size`` unit tests, so a regression here also indicates a
    drift from the upstream Move test table.
    """

    @pytest.mark.parametrize(
        ("unencoded_length", "n_shards", "expected"),
        [
            # redstuff.move test_encoded_size_reed_solomon(): n_shards=10,
            # unencoded_length=1 -> symbol_size=2, slivers_size=2*(4+7)=22,
            # metadata=10*64+32=672, total=10*(22+672)=6940.
            (1, 10, 6940),
            # redstuff.move test_encoded_size_reed_solomon(): n_shards=10,
            # unencoded_length=(4*7)*100=2800 -> symbol_size=100 (exact,
            # even), slivers_size=(4+7)*100=1100, metadata=672,
            # total=10*(1100+672)=17720.
            (2800, 10, 17720),
            # Hand-derived from the existing symbol_size hand-computed table
            # (blob_length=113, n_shards=10 -> symbol_size=6): slivers_size
            # =(4+7)*6=66, metadata=672, total=10*(66+672)=7380.
            (113, 10, 7380),
            # redstuff.move test_encoded_size_reed_solomon(): n_shards=1000,
            # unencoded_length=1 -> symbol_size=2, slivers_size=
            # 2*(334+667)=2002, metadata=1000*64+32=64032,
            # total=1000*(2002+64032)=66_034_000.
            (1, 1000, 66_034_000),
            # redstuff.move test_encoded_size_reed_solomon(): n_shards=1000,
            # unencoded_length=(334*667)*100=22_277_800 -> symbol_size=100
            # (exact, even), slivers_size=(334+667)*100=100100,
            # metadata=64032, total=1000*(100100+64032)=164_132_000.
            (22_277_800, 1000, 164_132_000),
            # n_shards=1000, unencoded_length=500_000_000 (500 MB):
            # n_symbols=334*667=222_778, symbol_size=ceil(500_000_000 /
            # 222_778)=2245 -> rounded up to even = 2246, slivers_size=
            # (334+667)*2246=2_248_246, metadata=64032,
            # total=1000*(2_248_246+64_032)=2_312_278_000.
            (500_000_000, 1000, 2_312_278_000),
        ],
    )
    def test_hand_computed_cases(
        self, unencoded_length: int, n_shards: int, expected: int
    ) -> None:
        """encoded_blob_length matches values hand-derived from redstuff.move."""
        assert (
            encoded_blob_length(unencoded_length=unencoded_length, n_shards=n_shards)
            == expected
        )

    def test_zero_length_matches_length_one(self) -> None:
        """A zero-length blob is treated as length 1, matching redstuff.move's
        ``symbol_size`` (which substitutes ``unencoded_length = 1`` when 0)
        and its own ``test_zero_size`` unit test."""
        assert encoded_blob_length(
            unencoded_length=0, n_shards=10
        ) == encoded_blob_length(unencoded_length=1, n_shards=10)
        assert encoded_blob_length(unencoded_length=0, n_shards=10) == 6940

    @pytest.mark.parametrize(
        ("unencoded_length", "n_shards"),
        [(1, 7), (113, 10), (100_000, 31), (500_000_000, 1000)],
    )
    def test_strictly_greater_than_unencoded_length(
        self, unencoded_length: int, n_shards: int
    ) -> None:
        """Erasure-coded encoding always expands a non-trivial blob."""
        assert (
            encoded_blob_length(unencoded_length=unencoded_length, n_shards=n_shards)
            > unencoded_length
        )

    def test_expansion_ratio_n_shards_1000_is_plausible(self) -> None:
        """For a blob large enough that fixed per-shard metadata is a small
        fraction of the total, the expansion ratio (encoded / unencoded)
        lands in the documented ~4.5x-5x Walrus replication range,
        including metadata overhead. A RANGE is asserted, not an exact
        ratio, since the exact ratio depends on symbol-size rounding.
        """
        unencoded_length = 500_000_000
        n_shards = 1000
        total = encoded_blob_length(
            unencoded_length=unencoded_length, n_shards=n_shards
        )
        ratio = total / unencoded_length
        assert 4.5 <= ratio <= 5.0


class TestUrlBase64:
    """URL-safe, unpadded base64 for storage-node URL paths."""

    def test_no_padding_or_url_unsafe_characters(self) -> None:
        """The encoded form carries no '=' padding and no '+' or '/'."""
        blob_id = bytes(range(32))
        encoded = blob_id_to_url_base64(blob_id=blob_id)
        assert "=" not in encoded
        assert "+" not in encoded
        assert "/" not in encoded

    def test_round_trip(self) -> None:
        """Decoding the encoded form returns the original 32-byte value."""
        blob_id = bytes(range(32))
        encoded = blob_id_to_url_base64(blob_id=blob_id)
        assert blob_id_from_url_base64(value=encoded) == blob_id

    def test_alphabet_differs_from_standard_base64(self) -> None:
        """A blob_id that hits '+'/'/' in standard base64 uses '-'/'_' here.

        Three 0xFF bytes encode as '////' in the standard alphabet and as
        '____' in the URL-safe alphabet -- the two are not interchangeable.
        """
        blob_id = bytes([0xFF, 0xFF, 0xFF])
        url_safe = blob_id_to_url_base64(blob_id=blob_id)
        assert url_safe == "____"
        assert "/" not in url_safe


class TestStandardBase64:
    """Standard, padded base64 for confirmation response bodies."""

    def test_decodes_padded_standard_alphabet_string(self) -> None:
        """A standard, padded base64 string decodes to its original bytes."""
        original = b"hello world!!"
        encoded = "aGVsbG8gd29ybGQhIQ=="
        assert decode_standard_base64(value=encoded) == original

    def test_standard_alphabet_differs_from_url_safe(self) -> None:
        """The same bytes that give '////' standard give '____' URL-safe."""
        blob_id = bytes([0xFF, 0xFF, 0xFF])
        standard_encoded = "////"
        url_safe_encoded = blob_id_to_url_base64(blob_id=blob_id)
        assert standard_encoded != url_safe_encoded
        assert decode_standard_base64(value=standard_encoded) == blob_id


class TestU256Conversions:
    """Little-endian u256 conversion for blob IDs and root hashes."""

    def test_blob_id_little_endian_low_byte(self) -> None:
        """A 1 in the first byte is the value 1 (little-endian)."""
        value = bytes([1]) + bytes(31)
        assert blob_id_to_u256(blob_id=value) == 1

    def test_blob_id_little_endian_high_byte(self) -> None:
        """A 1 in the last byte is the value 1 << 248 (little-endian)."""
        value = bytes(31) + bytes([1])
        assert blob_id_to_u256(blob_id=value) == 1 << 248

    def test_root_hash_little_endian_low_byte(self) -> None:
        """Root hash conversion uses the same little-endian convention."""
        value = bytes([1]) + bytes(31)
        assert root_hash_to_u256(root_hash=value) == 1

    def test_root_hash_little_endian_high_byte(self) -> None:
        """Root hash conversion uses the same little-endian convention."""
        value = bytes(31) + bytes([1])
        assert root_hash_to_u256(root_hash=value) == 1 << 248

    def test_blob_id_wrong_length_raises(self) -> None:
        """A blob_id that is not exactly 32 bytes is rejected."""
        with pytest.raises(ValueError):
            blob_id_to_u256(blob_id=bytes(31))

    def test_root_hash_wrong_length_raises(self) -> None:
        """A root_hash that is not exactly 32 bytes is rejected."""
        with pytest.raises(ValueError):
            root_hash_to_u256(root_hash=bytes(33))


class TestEncodeBlob:
    """RedStuff encoding entry point."""

    def test_zero_shards_raises(self) -> None:
        """n_shards=0 is rejected before any encoding is attempted."""
        with pytest.raises(ValueError):
            encode_blob(data=b"hello", n_shards=0)

    def test_oversize_blob_raises_without_large_allocation(self) -> None:
        """A blob just over the limit raises BlobTooLargeError.

        n_shards=1 keeps max_blob_size small (65_534 bytes), so this test
        does not need to allocate anywhere near the multi-gigabyte limits
        seen at realistic shard counts.
        """
        limit = max_blob_size(n_shards=1)
        oversize = bytes(limit + 1)
        with pytest.raises(BlobTooLargeError):
            encode_blob(data=oversize, n_shards=1)

    def test_round_trip_small_blob(self) -> None:
        """A small blob encodes into one sliver pair per shard."""
        data = b"pytusk native upload round trip test payload"
        n_shards = 7
        encoded = encode_blob(data=data, n_shards=n_shards)
        assert isinstance(encoded, EncodedBlob)
        assert len(encoded.slivers) == n_shards
        assert {pair.sliver_pair_index for pair in encoded.slivers} == set(
            range(n_shards)
        )
        assert len(encoded.blob_id) == 32
        assert len(encoded.root_hash) == 32
        assert encoded.unencoded_length == len(data)
        assert encoded.n_shards == n_shards

    def test_round_trip_modest_shard_count(self) -> None:
        """A slightly larger shard count still yields one pair per shard."""
        data = b"a different payload for the ten shard case"
        n_shards = 10
        encoded = encode_blob(data=data, n_shards=n_shards)
        assert len(encoded.slivers) == n_shards
        assert {pair.sliver_pair_index for pair in encoded.slivers} == set(
            range(n_shards)
        )


class TestRotationOffset:
    """Per-blob shard-rotation offset, including the endianness convention."""

    def test_big_endian_not_little_endian(self) -> None:
        """REGRESSION: the offset reads the blob ID as BIG-endian.

        Upstream's ``bytes_mod`` folds bytes most-significant-first. Flipping
        to little-endian is the highest-risk defect on the read path: it still
        yields a plausible in-range offset, and the wrong shard mapping that
        follows produces no error. This blob ID is chosen so the two
        conventions disagree -- big-endian gives 1, little-endian gives 4.
        """
        blob_id = b"\x00" * 31 + b"\x01"
        assert rotation_offset(blob_id=blob_id, n_shards=7) == 1
        assert rotation_offset(blob_id=blob_id, n_shards=7) != 4

    def test_big_endian_mirrored_blob_id(self) -> None:
        """The byte-mirrored blob ID gives the mirrored disagreement."""
        blob_id = b"\x01" + b"\x00" * 31
        assert rotation_offset(blob_id=blob_id, n_shards=7) == 4

    @pytest.mark.parametrize("n_shards", [1, 3, 7, 10, 100, 1000])
    def test_offset_is_in_range(self, n_shards: int) -> None:
        """The offset is always reduced into [0, n_shards)."""
        offset = rotation_offset(blob_id=bytes(range(32)), n_shards=n_shards)
        assert 0 <= offset < n_shards

    def test_rejects_wrong_blob_id_length(self) -> None:
        """A blob ID that is not exactly 32 bytes is rejected."""
        with pytest.raises(ValueError, match="blob_id must be 32 bytes"):
            rotation_offset(blob_id=b"\x00" * 31, n_shards=10)

    def test_rejects_non_positive_shard_count(self) -> None:
        """n_shards must be positive -- it is NonZeroU16 upstream."""
        with pytest.raises(ValueError, match="n_shards must be positive"):
            rotation_offset(blob_id=b"\x00" * 32, n_shards=0)


class TestShardPairIndexRotation:
    """Shard index <-> sliver-pair index conversion."""

    @pytest.mark.parametrize("n_shards", [1, 3, 7, 10, 100, 1000])
    def test_round_trip_is_identity_for_every_shard(self, n_shards: int) -> None:
        """shard -> pair -> shard returns the original index, for every shard."""
        blob_id = bytes(range(32))
        for shard_index in range(n_shards):
            pair_index = shard_index_to_pair_index(
                shard_index=shard_index, blob_id=blob_id, n_shards=n_shards
            )
            assert (
                pair_index_to_shard_index(
                    pair_index=pair_index, blob_id=blob_id, n_shards=n_shards
                )
                == shard_index
            )

    @pytest.mark.parametrize("n_shards", [1, 3, 7, 10, 100])
    def test_mapping_is_a_bijection(self, n_shards: int) -> None:
        """Every shard maps to a distinct pair index covering the full range."""
        blob_id = bytes(range(32))
        pair_indices = {
            shard_index_to_pair_index(
                shard_index=shard_index, blob_id=blob_id, n_shards=n_shards
            )
            for shard_index in range(n_shards)
        }
        assert pair_indices == set(range(n_shards))

    def test_matches_upstream_formula(self) -> None:
        """Both directions match the upstream arithmetic for a known offset."""
        blob_id = b"\x00" * 31 + b"\x05"
        n_shards = 10
        assert rotation_offset(blob_id=blob_id, n_shards=n_shards) == 5
        assert (
            shard_index_to_pair_index(
                shard_index=0, blob_id=blob_id, n_shards=n_shards
            )
            == 5
        )
        assert (
            shard_index_to_pair_index(
                shard_index=7, blob_id=blob_id, n_shards=n_shards
            )
            == 2
        )
        assert (
            pair_index_to_shard_index(
                pair_index=5, blob_id=blob_id, n_shards=n_shards
            )
            == 0
        )
        assert (
            pair_index_to_shard_index(
                pair_index=2, blob_id=blob_id, n_shards=n_shards
            )
            == 7
        )

    def test_zero_offset_is_identity(self) -> None:
        """A blob ID whose offset is zero leaves both mappings as identity."""
        blob_id = b"\x00" * 32
        n_shards = 10
        assert rotation_offset(blob_id=blob_id, n_shards=n_shards) == 0
        for index in range(n_shards):
            assert (
                shard_index_to_pair_index(
                    shard_index=index, blob_id=blob_id, n_shards=n_shards
                )
                == index
            )
            assert (
                pair_index_to_shard_index(
                    pair_index=index, blob_id=blob_id, n_shards=n_shards
                )
                == index
            )

    def test_rejects_out_of_range_shard_index(self) -> None:
        """A shard index at or beyond n_shards is rejected."""
        with pytest.raises(ValueError, match=r"shard_index must be in \[0, 10\)"):
            shard_index_to_pair_index(
                shard_index=10, blob_id=b"\x00" * 32, n_shards=10
            )

    def test_rejects_negative_shard_index(self) -> None:
        """A negative shard index is rejected rather than wrapping."""
        with pytest.raises(ValueError, match=r"shard_index must be in \[0, 10\)"):
            shard_index_to_pair_index(
                shard_index=-1, blob_id=b"\x00" * 32, n_shards=10
            )

    def test_rejects_out_of_range_pair_index(self) -> None:
        """A pair index at or beyond n_shards is rejected."""
        with pytest.raises(ValueError, match=r"pair_index must be in \[0, 10\)"):
            pair_index_to_shard_index(
                pair_index=10, blob_id=b"\x00" * 32, n_shards=10
            )


class TestVerifyBlobMetadata:
    """Verification of BCS blob metadata returned by a storage node."""

    def test_round_trip_from_encode_result(self) -> None:
        """Metadata built from an encode result verifies and matches its blob ID.

        The outer ``BlobMetadataWithId`` that verification expects is the
        encode result's blob ID followed by its inner ``BlobMetadata``.
        """
        data = b"payload for the metadata verification round trip"
        n_shards = 10
        encoded = encode_blob(data=data, n_shards=n_shards)
        outer = encoded.blob_id + encoded.metadata_bcs
        metadata = verify_blob_metadata(metadata_bcs=outer, n_shards=n_shards)
        assert isinstance(metadata, VerifiedBlobMetadata)
        assert metadata.blob_id == encoded.blob_id
        assert metadata.unencoded_length == len(data)
        assert metadata.n_shards == n_shards

    def test_inner_metadata_is_rejected(self) -> None:
        """REGRESSION: the INNER BlobMetadata is not the outer form.

        ``EncodedBlob.metadata_bcs`` is what a metadata PUT sends; verification
        needs the outer form, which additionally carries a leading 32-byte blob
        ID. Passing the inner form is a documented footgun.
        """
        encoded = encode_blob(data=b"inner form is not accepted", n_shards=10)
        with pytest.raises(MetadataVerificationError):
            verify_blob_metadata(metadata_bcs=encoded.metadata_bcs, n_shards=10)

    def test_garbage_is_rejected(self) -> None:
        """Unparseable metadata bytes raise MetadataVerificationError."""
        with pytest.raises(MetadataVerificationError):
            verify_blob_metadata(metadata_bcs=b"\x00" * 64, n_shards=10)

    def test_rejects_shard_count_below_one(self) -> None:
        """n_shards must be at least 1."""
        with pytest.raises(ValueError, match="n_shards must be at least 1"):
            verify_blob_metadata(metadata_bcs=b"", n_shards=0)


class TestDecodeBlob:
    """Reconstruction of a blob from slivers of a single axis."""

    @staticmethod
    def _encoded_with_metadata(
        data: bytes, n_shards: int
    ) -> tuple[EncodedBlob, VerifiedBlobMetadata]:
        """Encode data and return it alongside its verified metadata."""
        encoded = encode_blob(data=data, n_shards=n_shards)
        metadata = verify_blob_metadata(
            metadata_bcs=encoded.blob_id + encoded.metadata_bcs,
            n_shards=n_shards,
        )
        return encoded, metadata

    def test_verified_round_trip(self) -> None:
        """Encode then decode with verification returns the original bytes."""
        data = b"a payload that survives the verified decode round trip"
        encoded, metadata = self._encoded_with_metadata(data, 10)
        slivers = [pair.primary for pair in encoded.slivers]
        assert decode_blob(slivers=slivers, metadata=metadata, axis="primary") == data

    def test_unverified_round_trip(self) -> None:
        """The unverified path returns the same bytes for good slivers."""
        data = b"a payload that survives the unverified decode round trip"
        encoded, metadata = self._encoded_with_metadata(data, 10)
        slivers = [pair.primary for pair in encoded.slivers]
        assert (
            decode_blob(
                slivers=slivers, metadata=metadata, axis="primary", verify=False
            )
            == data
        )

    def test_threshold_subset_is_sufficient(self) -> None:
        """A primary-axis subset at the decoding threshold still decodes.

        Sliver order does not matter and gaps are fine -- each sliver carries
        its own index in its bytes.
        """
        data = b"decoded from a threshold-sized subset of primary slivers"
        n_shards = 10
        encoded, metadata = self._encoded_with_metadata(data, n_shards)
        primary_threshold, _ = source_symbol_counts(n_shards=n_shards)
        slivers = [pair.primary for pair in encoded.slivers][:primary_threshold]
        assert decode_blob(slivers=slivers, metadata=metadata, axis="primary") == data

    def test_below_threshold_raises_decode_error(self) -> None:
        """Fewer slivers than the threshold cannot reconstruct the blob."""
        data = b"not enough slivers to reconstruct this payload"
        n_shards = 10
        encoded, metadata = self._encoded_with_metadata(data, n_shards)
        primary_threshold, _ = source_symbol_counts(n_shards=n_shards)
        slivers = [pair.primary for pair in encoded.slivers][: primary_threshold - 1]
        with pytest.raises(BlobDecodeError):
            decode_blob(slivers=slivers, metadata=metadata, axis="primary")

    def test_wrong_axis_surfaces_as_decode_error(self) -> None:
        """REGRESSION: wrong-axis slivers are DISCARDED, not rejected.

        Both axes share one BCS shape, so primary bytes passed as
        ``"secondary"`` deserialize, fail an internal length check, and are
        silently dropped -- surfacing as a decode failure that looks exactly
        like "not enough nodes answered".
        """
        data = b"primary slivers handed to the secondary axis"
        encoded, metadata = self._encoded_with_metadata(data, 10)
        slivers = [pair.primary for pair in encoded.slivers]
        with pytest.raises(BlobDecodeError):
            decode_blob(slivers=slivers, metadata=metadata, axis="secondary")

    def test_caller_bug_code_propagates_unwrapped(self) -> None:
        """REGRESSION: a caller-bug code is NOT wrapped as BlobDecodeError.

        Codes that indicate a pytusk defect rather than a bad node response
        must stay loud and untranslated. BlobDecodeError subclasses
        ValueError, so this asserts the concrete type as well.
        """
        data = b"an axis that does not exist"
        encoded, metadata = self._encoded_with_metadata(data, 10)
        slivers = [pair.primary for pair in encoded.slivers]
        with pytest.raises(ValueError) as excinfo:
            decode_blob(slivers=slivers, metadata=metadata, axis="diagonal")
        assert not isinstance(excinfo.value, BlobDecodeError)
        assert excinfo.value.args[0] == "invalid_axis"
