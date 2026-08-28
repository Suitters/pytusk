#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""RedStuff encoding for native Walrus upload, plus blob-ID and root-hash
conversions.

The actual RedStuff erasure coding is performed by the Rust extension
(:func:`pysui_fastcrypto.redstuff_encode`) -- this module wraps that call with
the size math the upstream Walrus crate performs before encoding, and with the
byte/base64/int conversions needed to talk to storage nodes and to Move.

Two base64 alphabets are in play across native upload and are NEVER
interchangeable: URL-safe unpadded (storage-node URL paths) and standard
padded (confirmation response bodies). See :func:`blob_id_to_url_base64`,
:func:`blob_id_from_url_base64` and :func:`decode_standard_base64` for the
split. Mixing them produces failures that look like signature corruption.

This module does not implement shard rotation. The Rust encoder already
returns shard-aligned slivers (``RedstuffEncodeResult.slivers`` is indexed by
shard, not by sliver-pair position), so pytusk has no need to re-derive the
rotation and none of these helpers attempt it.
"""

import base64
import dataclasses
import math

from pysui_fastcrypto import RedstuffSliverPair, redstuff_encode

__all__ = [
    "RS2_ENCODING_TYPE",
    "RS2_MAX_SYMBOL_SIZE",
    "RS2_REQUIRED_ALIGNMENT",
    "BlobTooLargeError",
    "EncodedBlob",
    "blob_id_from_url_base64",
    "blob_id_to_u256",
    "blob_id_to_url_base64",
    "decode_standard_base64",
    "encode_blob",
    "encoded_blob_length",
    "max_blob_size",
    "max_n_faulty",
    "metadata_length",
    "min_n_correct",
    "root_hash_to_u256",
    "source_symbol_counts",
    "symbol_size",
]

RS2_ENCODING_TYPE: int = 1
"""The only live ``EncodingType`` variant. Wire byte 0 is legacy RaptorQ and
is rejected upstream -- there is no fallback to select."""

RS2_MAX_SYMBOL_SIZE: int = 65534
"""Maximum RS2 symbol size in bytes: ``u16::MAX - 1``, NOT ``u16::MAX``. The
top value is reserved upstream and is not a usable symbol size."""

RS2_REQUIRED_ALIGNMENT: int = 2
"""RS2 symbol size must round up to a multiple of this value (an even
number)."""

_DIGEST_LEN: int = 32
"""Length in bytes of a single Red Stuff metadata hash. Mirrors the private
``DIGEST_LEN`` constant in ``redstuff.move`` (line 7)."""

_BLOB_ID_LEN: int = 32
"""Length in bytes of a blob ID as stored in the Red Stuff metadata. Mirrors
the private ``BLOB_ID_LEN`` constant in ``redstuff.move`` (line 10)."""


class BlobTooLargeError(ValueError):
    """Raised when a blob exceeds the maximum encodable size for the
    committee's shard count."""


def max_n_faulty(*, n_shards: int) -> int:
    """Return the maximum number of Byzantine-faulty shards ``f`` tolerated.

    Upstream rule: ``f = (n_shards - 1) // 3``.

    Args:
        n_shards (int): Total shard count for the committee.

    Returns:
        int: Maximum tolerated faulty shard count.
    """
    return (n_shards - 1) // 3


def min_n_correct(*, n_shards: int) -> int:
    """Return the minimum number of correct (non-faulty) shards.

    Upstream rule: ``n_shards - f``, i.e. ``n_shards - max_n_faulty(...)``.

    Args:
        n_shards (int): Total shard count for the committee.

    Returns:
        int: Minimum guaranteed-correct shard count.
    """
    return n_shards - max_n_faulty(n_shards=n_shards)


def source_symbol_counts(*, n_shards: int) -> tuple[int, int]:
    """Return the ``(primary, secondary)`` source symbol counts for a shard count.

    Upstream rule: ``secondary = n_shards - f`` and ``primary = n_shards - 2f``,
    where ``f = max_n_faulty(n_shards=n_shards)``.

    CRITICAL: this is ``n_shards - 2f`` / ``n_shards - f``, NOT ``f+1`` /
    ``2f+1``. The two formulas agree only when ``n_shards % 3 == 1``. Do not
    "simplify" this to ``f+1`` / ``2f+1`` -- that is wrong for any
    ``n_shards`` not congruent to 1 mod 3. For example ``n_shards=6`` gives
    ``f=1`` and ``primary=4``, not the ``f+1=2`` an approximation would
    produce.

    Args:
        n_shards (int): Total shard count for the committee.

    Returns:
        tuple[int, int]: ``(primary, secondary)`` source symbol counts.
    """
    secondary = min_n_correct(n_shards=n_shards)
    primary = secondary - max_n_faulty(n_shards=n_shards)
    return primary, secondary


def max_blob_size(*, n_shards: int) -> int:
    """Return the maximum blob size, in bytes, encodable for a shard count.

    ``primary * secondary * RS2_MAX_SYMBOL_SIZE``. No fixed constant exists
    upstream for this value -- it is always derived from the live shard count,
    because the shard count varies between Walrus deployments and across
    committee changes. At ``n_shards=1000`` this evaluates to
    ``14_599_533_452`` bytes (~13.6 GiB).

    Args:
        n_shards (int): Total shard count for the committee.

    Returns:
        int: Maximum encodable blob size in bytes.
    """
    primary, secondary = source_symbol_counts(n_shards=n_shards)
    return primary * secondary * RS2_MAX_SYMBOL_SIZE


def symbol_size(*, blob_length: int, n_shards: int) -> int:
    """Return the RS2 symbol size, in bytes, for a blob and shard count.

    Computed as ``ceil(max(blob_length, 1) / (primary * secondary))``, then
    rounded UP to the next multiple of :data:`RS2_REQUIRED_ALIGNMENT`. An
    empty blob is treated as length 1 so the result is never zero -- Walrus
    still encodes and stores zero-length blobs, and a zero symbol size would
    be nonsensical.

    Args:
        blob_length (int): Length of the blob in bytes.
        n_shards (int): Total shard count for the committee.

    Returns:
        int: Symbol size in bytes, a positive even number.
    """
    primary, secondary = source_symbol_counts(n_shards=n_shards)
    effective_length = max(blob_length, 1)
    raw = math.ceil(effective_length / (primary * secondary))
    remainder = raw % RS2_REQUIRED_ALIGNMENT
    if remainder:
        raw += RS2_REQUIRED_ALIGNMENT - remainder
    return raw


def metadata_length(*, n_shards: int) -> int:
    """Return the size, in bytes, of the per-shard Red Stuff metadata.

    Mirrors ``metadata_size`` in ``redstuff.move`` (lines 60-62):
    ``(n_shards as u64) * DIGEST_LEN * 2 + BLOB_ID_LEN`` -- two sliver root
    hashes (primary and secondary) per shard, plus one blob ID. The MOVE
    version is authoritative: ``reserve_space``'s ``storage_amount`` is
    charged on-chain against the Move contract's own computation, so any
    future change to this formula must be re-checked against
    ``redstuff.move``, not the Rust ``walrus_core`` crate -- matching the
    Rust implementation but not the Move one would still abort the
    transaction.

    Args:
        n_shards (int): Total shard count for the committee.

    Returns:
        int: Metadata size in bytes.
    """
    return n_shards * _DIGEST_LEN * 2 + _BLOB_ID_LEN


def encoded_blob_length(*, unencoded_length: int, n_shards: int) -> int:
    """Return the total ENCODED size, in bytes, of a blob under Red Stuff.

    Mirrors ``encoded_blob_length`` in ``redstuff.move`` (lines 17-25)::

        let slivers_size = (source_symbols_primary(n_shards) as u64
            + (source_symbols_secondary(n_shards) as u64))
            * (symbol_size(unencoded_length, n_shards) as u64);
        (n_shards as u64) * (slivers_size + metadata_size(n_shards))

    i.e. ``n_shards * ((primary + secondary) * symbol_size + metadata_length)``.
    The output includes the size of the metadata hashes and the blob ID
    (:func:`metadata_length`), replicated once per shard exactly as Move
    replicates it (``metadata_size(n_shards)`` is itself multiplied by
    ``n_shards`` a second time via the outer ``n_shards * (...)``).

    This reuses :func:`source_symbol_counts` and :func:`symbol_size`
    unchanged -- their arithmetic was checked against ``symbol_size`` in
    ``redstuff.move`` (lines 46-57) and ``source_symbols_primary`` /
    ``source_symbols_secondary`` (lines 28-35) and matches exactly, so no
    separate computation was introduced here.

    The MOVE version is authoritative: ``reserve_space``'s
    ``storage_amount`` argument is charged on-chain against the Move
    contract's own computation of this value, not against
    ``crates/walrus_core/encoding/config.rs``'s Rust implementation (which
    the Move docstring notes should be kept in sync, but is not what the
    chain actually enforces). Any future change to this formula must be
    re-checked against ``redstuff.move``, not the Rust source.

    Args:
        unencoded_length (int): Length in bytes of the original, unencoded
            blob.
        n_shards (int): Total shard count for the committee.

    Returns:
        int: Total encoded size in bytes, including per-shard metadata.
    """
    primary, secondary = source_symbol_counts(n_shards=n_shards)
    sliver_symbol_size = symbol_size(blob_length=unencoded_length, n_shards=n_shards)
    slivers_size = (primary + secondary) * sliver_symbol_size
    return n_shards * (slivers_size + metadata_length(n_shards=n_shards))


def blob_id_to_url_base64(*, blob_id: bytes) -> str:
    """Encode a blob ID as URL-safe, unpadded base64.

    This is the form used in storage-node URL PATHS. It is a DIFFERENT
    alphabet from :func:`decode_standard_base64`; mixing them produces
    failures that look like signature corruption. Never merge these helpers.

    Args:
        blob_id (bytes): Raw blob ID bytes.

    Returns:
        str: URL-safe base64 with ``=`` padding stripped.
    """
    return base64.urlsafe_b64encode(blob_id).decode("ascii").rstrip("=")


def blob_id_from_url_base64(*, value: str) -> bytes:
    """Decode a URL-safe, unpadded base64 blob ID.

    Inverse of :func:`blob_id_to_url_base64`. Re-adds ``=`` padding as needed
    before decoding. This is the URL-safe alphabet, NOT the standard alphabet
    used by :func:`decode_standard_base64` -- mixing the two alphabets
    produces failures that look like signature corruption.

    Args:
        value (str): URL-safe base64 string, with or without padding.

    Returns:
        bytes: The decoded blob ID bytes.
    """
    padding_needed = (-len(value)) % 4
    return base64.urlsafe_b64decode(value + "=" * padding_needed)


def decode_standard_base64(*, value: str) -> bytes:
    """Decode a standard, padded base64 string.

    This is the form used for ``serializedMessage`` and ``signature`` in
    confirmation RESPONSE BODIES. It is a DIFFERENT alphabet from
    :func:`blob_id_to_url_base64` / :func:`blob_id_from_url_base64`; mixing
    them produces failures that look like signature corruption. Never merge
    these helpers.

    Args:
        value (str): Standard, padded base64 string.

    Returns:
        bytes: The decoded bytes.
    """
    return base64.b64decode(value)


def blob_id_to_u256(*, blob_id: bytes) -> int:
    """Convert a raw 32-byte blob ID into its Move ``u256`` value.

    Move's ``u256`` BCS wire format is little-endian, and ``blob.move``
    asserts ``derive_blob_id(root_hash, encoding_type, size) == blob_id``,
    which pins this convention -- it is not a convention pytusk chose.

    A DIFFERENT, deliberately opposite convention applies elsewhere: the
    shard-rotation offset upstream uses BIG-endian on these same bytes.
    pytusk does not implement rotation -- the Rust ``redstuff_encode``
    already returns shard-aligned slivers -- so no big-endian helper belongs
    in this module. Do not add one for symmetry.

    Args:
        blob_id (bytes): Raw blob ID, must be exactly 32 bytes.

    Returns:
        int: The little-endian ``u256`` value.

    Raises:
        ValueError: If ``blob_id`` is not exactly 32 bytes.
    """
    if len(blob_id) != 32:
        raise ValueError(f"blob_id must be 32 bytes, got {len(blob_id)}")
    return int.from_bytes(blob_id, "little")


def root_hash_to_u256(*, root_hash: bytes) -> int:
    """Convert a raw 32-byte Merkle root hash into its Move ``u256`` value.

    Move's ``u256`` BCS wire format is little-endian, and ``blob.move``
    asserts ``derive_blob_id(root_hash, encoding_type, size) == blob_id``,
    which pins this convention -- it is not a convention pytusk chose.

    A DIFFERENT, deliberately opposite convention applies elsewhere: the
    shard-rotation offset upstream uses BIG-endian on these same bytes.
    pytusk does not implement rotation -- the Rust ``redstuff_encode``
    already returns shard-aligned slivers -- so no big-endian helper belongs
    in this module. Do not add one for symmetry.

    Args:
        root_hash (bytes): Raw root hash, must be exactly 32 bytes.

    Returns:
        int: The little-endian ``u256`` value.

    Raises:
        ValueError: If ``root_hash`` is not exactly 32 bytes.
    """
    if len(root_hash) != 32:
        raise ValueError(f"root_hash must be 32 bytes, got {len(root_hash)}")
    return int.from_bytes(root_hash, "little")


@dataclasses.dataclass(kw_only=True, frozen=True)
class EncodedBlob:
    """The result of RedStuff-encoding a blob for native Walrus upload.

    ``slivers[i]`` is the sliver pair for shard ``i`` -- the Rust encoder has
    already applied the blob-ID rotation, so index ``i`` here is a shard
    index, not a sliver-pair index. Each pair carries its own
    ``sliver_pair_index`` (see :class:`~pysui_fastcrypto.RedstuffSliverPair`),
    which is the value that belongs in the sliver PUT URL path and is NOT the
    shard index -- the two differ by the blob-ID-dependent rotation.

    Attributes:
        blob_id (bytes): Raw 32-byte blob ID.
        root_hash (bytes): Raw 32-byte Merkle root over the sliver metadata.
        unencoded_length (int): Length in bytes of the original, unencoded
            blob.
        n_shards (int): Total shard count the encoding was produced for.
        slivers (tuple[RedstuffSliverPair, ...]): Per-shard sliver pairs,
            indexed by shard.
        metadata_bcs (bytes): BCS-encoded Walrus ``BlobMetadata`` payload.
            A storage node requires this to be PUT to it (see
            :class:`~pytusk.commands.node_commands.PutMetadata`) before it
            will accept any sliver PUT for this blob.
    """

    blob_id: bytes
    root_hash: bytes
    unencoded_length: int
    n_shards: int
    slivers: tuple[RedstuffSliverPair, ...]
    metadata_bcs: bytes

    @property
    def blob_id_base64(self) -> str:
        """Return the blob ID as URL-safe, unpadded base64.

        Returns:
            str: The blob ID in the alphabet used for storage-node URL paths.
        """
        return blob_id_to_url_base64(blob_id=self.blob_id)

    @property
    def blob_id_u256(self) -> int:
        """Return the blob ID as a Move ``u256`` value.

        Returns:
            int: The little-endian ``u256`` value of the blob ID.
        """
        return blob_id_to_u256(blob_id=self.blob_id)

    @property
    def root_hash_u256(self) -> int:
        """Return the root hash as a Move ``u256`` value.

        Returns:
            int: The little-endian ``u256`` value of the root hash.
        """
        return root_hash_to_u256(root_hash=self.root_hash)


def encode_blob(*, data: bytes, n_shards: int) -> EncodedBlob:
    """RedStuff-encode a blob for a committee of the given shard count.

    The blob size is checked against :func:`max_blob_size` BEFORE encoding is
    attempted. This guard must run first: upstream would otherwise fail later
    with an opaque overflow deep inside the encoder, and in the full native
    upload flow this check must happen before any WAL is spent registering
    storage for a blob that can never be encoded.

    Args:
        data (bytes): The unencoded blob content.
        n_shards (int): Total shard count of the target committee.

    Returns:
        EncodedBlob: The encoded blob, including its slivers, blob ID and
        root hash.

    Raises:
        ValueError: If ``n_shards`` is less than 1.
        BlobTooLargeError: If ``data`` exceeds the maximum size encodable for
            ``n_shards``.
    """
    if n_shards < 1:
        raise ValueError(f"n_shards must be at least 1, got {n_shards}")
    limit = max_blob_size(n_shards=n_shards)
    if len(data) > limit:
        raise BlobTooLargeError(
            f"Blob of {len(data)} bytes exceeds the maximum encodable size of "
            f"{limit} bytes for a committee of {n_shards} shards"
        )
    result = redstuff_encode(data, n_shards)
    return EncodedBlob(
        blob_id=result.blob_id,
        root_hash=result.root_hash,
        unencoded_length=len(data),
        n_shards=n_shards,
        slivers=tuple(result.slivers),
        # `RedstuffEncodeResult.metadata_bcs` is a DELIBERATE hard
        # dependency on pysui_fastcrypto exposing this property: no
        # getattr fallback or default is used, so this line raises
        # AttributeError against any wheel that lacks it, rather than
        # silently degrading. See PutMetadata/native-upload's
        # metadata-stage work for why this field is required.
        metadata_bcs=result.metadata_bcs,
    )
