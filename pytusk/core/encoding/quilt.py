#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus quilt assembly: the BCS index types, and the packing that uses them.

A quilt is many small blobs packed into ONE Walrus blob. Upstream assembles it
client-side, and so must pytusk -- a Walrus publisher does the packing for you,
but no public publisher exists on mainnet, so the relay path has to hand the
relay an already-assembled buffer.

WHAT IS AND IS NOT BCS HERE. Only two islands of the quilt are BCS-encoded:
the quilt index (:class:`QuiltIndexV1`), and, inside each packed blob, its
identifier and tags. Everything framing those islands -- the 6-byte per-blob
header, the 2-byte identifier and tag size prefixes, the 4-byte index size --
is hand-rolled FIXED-WIDTH LITTLE-ENDIAN, not BCS (upstream writes it with
``to_le_bytes``/``from_le_bytes``). The two are easy to conflate and the
result is a quilt that encodes and uploads cleanly but cannot be read back:
each BCS island already carries its OWN ULEB128 length, so emitting the outer
fixed-width prefix with a BCS/canoser vector type would write a SECOND length
and corrupt the stream. Same failure class as the ``AuthPackage`` note in
:mod:`pytusk.core.ops.tip_compose`.

FIELD ORDER IS THE WIRE FORMAT. BCS is not self-describing -- no field name
is ever serialized -- so a struct's bytes are just its fields concatenated in
DECLARATION order. Reordering ``_fields`` below silently breaks interop with
every other Walrus client. (Upstream also carries
``#[serde(rename_all = "camelCase")]``; that affects only their JSON and has
no bearing on BCS, precisely because names never reach the wire.)

``start_index`` is deliberately absent from :class:`QuiltPatchV1`. Upstream
marks it ``#[serde(skip)]`` and rebuilds it on read by chaining each patch's
``end_index``, so serializing it would inject bytes no reader expects.
:class:`~pytusk.core.types.quilts.QuiltPatchLayout` still carries it, because
composing a patch's ``QuiltPatchId`` needs it.

THE LAYOUT IS A COLUMN-MAJOR MATRIX. The quilt is ``n_rows x n_columns``
symbols of ``symbol_size`` bytes, where ``(n_rows, n_columns)`` are the
RedStuff ``(primary, secondary)`` source-symbol counts for the committee's
shard count -- so a quilt's geometry is only meaningful against the
``n_shards`` it was built for. Each blob occupies WHOLE consecutive columns
and is written down the rows of a column before moving to the next, which is
what lets a reader fetch one patch's columns without pulling the whole quilt.
The index itself is written as the blob at column zero.
"""

import base64
import math
import types
import typing
import unicodedata
from collections.abc import Mapping

import pysui.sui.sui_bcs.bcs_stnd as bcse
import pysui.sui.sui_bcs.pysui_bcs as pbcsbase

from pytusk.core.encoding.redstuff import (
    RS2_MAX_SYMBOL_SIZE,
    RS2_REQUIRED_ALIGNMENT,
    source_symbol_counts,
)
from pytusk.core.types.quilts import AssembledQuilt, QuiltPatchInput, QuiltPatchLayout

QUILT_BLOB_ATTRIBUTES: Mapping[str, str] = types.MappingProxyType(
    {"_walrusBlobType": "quilt"}
)
"""On-chain metadata marking a registered ``Blob`` as holding a quilt.

Upstream's ``reserve_and_store_quilt`` writes this pair unconditionally, and
readers rely on it to tell a quilt from an ordinary blob -- the bytes alone
cannot, since an assembled quilt IS an ordinary blob to every layer beneath
this one. Written INSIDE Tx1 via
:func:`~pytusk.core.ops.blob_compose.add_registration_sequence`'s
``attributes``, never as a follow-up transaction, which could fail after the
storage is already paid for and leave a quilt whose type is unset.

A read-only ``MappingProxyType`` so no caller can mutate the shared constant
out from under the next write.
"""

QUILT_VERSION_BYTE: int = 1
"""Version byte prefixing the quilt index and every packed blob's header."""

QUILT_VERSION_BYTES_LENGTH: int = 1
QUILT_INDEX_SIZE_BYTES_LENGTH: int = 4
QUILT_INDEX_PREFIX_SIZE: int = QUILT_VERSION_BYTES_LENGTH + QUILT_INDEX_SIZE_BYTES_LENGTH
"""Bytes preceding the serialized index: version byte + u32 LE index length."""

QUILT_PATCH_BLOB_HEADER_SIZE: int = 6
"""Per-blob header: version(1) + length(u32 LE) + mask(1)."""

QUILT_PATCH_LENGTH_BYTES_LENGTH: int = 4
MAX_SERIALIZED_BLOB_SIZE: int = (1 << (8 * QUILT_PATCH_LENGTH_BYTES_LENGTH)) - 1
"""4294967295 -- the largest payload the header's u32 LE length field holds.

Upstream bounds this as ``BlobHeaderV1::MAX_SERIALIZED_BLOB_SIZE`` (``u32::MAX``)
with a STRICT ``>``, so a payload of exactly this size is ACCEPTED. The
identifier ceiling one field over uses ``>=`` instead. That asymmetry is
upstream's own, and reproducing it is what keeps the two clients agreeing on
which inputs are valid.
"""

BLOB_IDENTIFIER_SIZE_BYTES_LENGTH: int = 2
TAGS_SIZE_BYTES_LENGTH: int = 2
MAX_BLOB_IDENTIFIER_BYTES_LENGTH: int = (1 << (8 * BLOB_IDENTIFIER_SIZE_BYTES_LENGTH)) - 1
"""65535 -- a BYTE ceiling, not a character one, set by the u16 size prefix."""

MAX_NUM_SLIVERS_FOR_QUILT_INDEX: int = 10
"""Upper bound on columns the index may occupy."""

HAS_TAGS_FLAG: int = 1
"""Header mask bit set when a packed blob carries tags."""


class QuiltAssemblyError(ValueError):
    """Raised when a set of blobs cannot be assembled into a valid quilt.

    ALWAYS PRE-SPEND. Assembly runs before any registration, so nothing has
    been paid for when this raises and there is no on-chain state to report
    -- which is why this is an exception rather than a receipt outcome.
    """


class QuiltTagEntry(pbcsbase.BCS_Struct):
    """One key/value tag pair inside a quilt patch's tag map.

    Upstream types the tag map as a Rust ``BTreeMap<String, String>``, which
    BCS-serializes as a ULEB128 entry count followed by the pairs themselves
    -- there is NO enclosing struct on the wire. This type is therefore the
    map's ELEMENT, and the map is a bare vector of it. A Move ``VecMap``
    wrapper would produce byte-identical output (a single-field struct adds
    nothing), but it is not what the source declares, so it is not modelled.
    """

    _fields = [  # noqa: RUF012 -- canoser class-level contract, never mutated
        ("key", bcse.String),
        ("value", bcse.String),
    ]


class QuiltPatchV1(pbcsbase.BCS_Struct):
    """One blob's entry in the serialized quilt index.

    Field order matches upstream's declaration order and IS the wire format
    -- see this module's docstring. Build instances with
    :meth:`from_layout` rather than by hand, so tag ordering cannot be got
    wrong.
    """

    _fields = [  # noqa: RUF012 -- canoser class-level contract, never mutated
        ("end_index", bcse.U16),
        ("identifier", bcse.String),
        ("tags", [QuiltTagEntry, None, True]),
    ]

    @classmethod
    def from_layout(cls, *, layout: QuiltPatchLayout) -> "QuiltPatchV1":
        """Build a patch entry from an assembled layout, sorting its tags.

        Args:
            layout (QuiltPatchLayout): The patch's assembled identity and
                column range. Only ``identifier``, ``tags`` and ``end_index``
                are read -- ``start_index`` is deliberately not serialized.

        Returns:
            QuiltPatchV1: The index entry, with tags in key order.
        """
        return cls(
            end_index=layout.end_index,
            identifier=layout.identifier,
            tags=sorted_tag_entries(tags=layout.tags),
        )


class QuiltIndexV1(pbcsbase.BCS_Struct):
    """The quilt's index: every patch's identity and where it sits.

    Stored INSIDE the quilt's own bytes as the blob at column zero, not
    alongside them -- a reader recovers it from the first columns of the
    assembled buffer.
    """

    _fields = [  # noqa: RUF012 -- canoser class-level contract, never mutated
        ("quilt_patches", [QuiltPatchV1, None, True]),
    ]


class _QuiltTagsBcs(pbcsbase.BCS_Struct):
    """Serialization vehicle for a STANDALONE tag map.

    A packed blob carries its tags inline, ahead of its contents, as a bare
    BCS map -- the same bytes the index carries, but not nested in any
    struct. canoser serializes a value through a type, so obtaining those
    bytes needs a type to hang the vector on. A single-field struct is
    exactly that and NOTHING MORE: a struct's BCS is its fields
    concatenated, so a one-field struct's bytes ARE its field's bytes. This
    adds no wrapper on the wire and is not a model of anything -- it is a
    handle for serializing the vector on its own.
    """

    _fields = [  # noqa: RUF012 -- canoser class-level contract, never mutated
        ("tags", [QuiltTagEntry, None, True]),
    ]


def sorted_tag_entries(*, tags: dict[str, str]) -> list[QuiltTagEntry]:
    """Return a tag mapping as BCS entries in canonical map order.

    THE SORT IS THE POINT, and it is the single implementation of it. BCS
    orders a map's entries by their SERIALIZED key bytes, and a serialized
    ``String`` is a ULEB128 byte-length prefix followed by UTF-8. The prefix
    therefore LEADS the comparison, so a shorter key sorts ahead of a longer
    one whatever its characters say: ``"b"`` (``01 62``) precedes ``"aa"``
    (``02 6161``), and ``"zeta"`` (``04 ...``) precedes ``"alpha"``
    (``05 ...``).

    Sorting by ``str`` instead is WRONG, and wrong in the way that survives
    review: the two orders AGREE whenever the keys share a length, so it
    looks correct locally and against any test whose tag keys happen to be
    the same size. It was caught only by pinning a reference vector with
    mixed-length keys. Verified against walrus CLI 1.54.0: for the tags
    ``{"aa": "1", "b": "2"}`` the reference blob id is reproduced by this
    ordering and by no other.

    Args:
        tags (dict[str, str]): The caller's tag mapping, in any order.

    Returns:
        list[QuiltTagEntry]: Entries in canonical BCS map order.
    """
    return [
        QuiltTagEntry(key=key, value=tags[key])
        for key in sorted(tags, key=lambda key: bcse.String.encode(key))
    ]


def _serialize_tags(*, tags: dict[str, str]) -> bytes:
    """Serialize a standalone tag map to its bare BCS bytes.

    Args:
        tags (dict[str, str]): The caller's tag mapping, in any order.

    Returns:
        bytes: ULEB128 entry count followed by key-sorted pairs.
    """
    return _QuiltTagsBcs(tags=sorted_tag_entries(tags=tags)).serialize()


def _can_blobs_fit(*, blob_sizes: list[int], n_columns: int, column_size: int) -> bool:
    """Report whether every blob fits when each takes WHOLE columns.

    Args:
        blob_sizes (list[int]): Serialized size of each blob, index first.
        n_columns (int): Columns available in the matrix.
        column_size (int): Bytes one column holds.

    Returns:
        bool: True when the total columns consumed fits the matrix.
    """
    if column_size <= 0:
        return False
    return (
        sum(math.ceil(size / column_size) for size in blob_sizes) <= n_columns
    )


def _div_ceil(*, numerator: int, denominator: int) -> int:
    """Integer ceiling division, mirroring Rust's ``usize::div_ceil``.

    Args:
        numerator (int): The dividend.
        denominator (int): The divisor, which must be positive.

    Returns:
        int: The smallest integer not less than the quotient.
    """
    return -(-numerator // denominator)


def compute_symbol_size(
    *,
    blob_sizes: list[int],
    n_columns: int,
    n_rows: int,
    max_index_columns: int = MAX_NUM_SLIVERS_FOR_QUILT_INDEX,
) -> int:
    """Return the smallest symbol size that fits every blob in whole columns.

    Binary search, mirroring upstream exactly. The lower bound is the largest
    of three CEILINGS -- the average bytes per symbol if everything packed
    perfectly, the index's own need given its column cap, and the space the
    index prefix alone requires -- and the search narrows until the smallest
    workable size is found, then rounds UP to the encoding's alignment.

    EVERY step is integer arithmetic, because upstream's is: its operands are
    all ``usize`` and it performs NO real-valued division anywhere in this
    function. Both bounds use ``div_ceil``, the midpoint uses truncating
    integer division, and the result uses ``next_multiple_of``. Note the
    upper bound's inner ``n_columns / len(blob_sizes)`` truncates BEFORE
    being multiplied by ``n_rows``, and the whole product is a single
    divisor -- not a division followed by a multiplication. Substituting
    float division anywhere here searches a different space and can settle on
    a symbol size no other client would choose, giving a quilt that is
    self-consistent but matches nobody else's bytes.

    Args:
        blob_sizes (list[int]): Serialized size of each blob. The INDEX's own
            size must be element zero -- the second lower bound depends on it.
        n_columns (int): Columns available, from the secondary symbol count.
        n_rows (int): Rows available, from the primary symbol count.
        max_index_columns (int): Cap on columns the index may occupy.

    Returns:
        int: The symbol size in bytes, aligned upward.

    Raises:
        QuiltAssemblyError: If there are no blobs, if there are more blobs
            than columns, or if no aligned symbol size within the encoding's
            maximum can hold them.
    """
    if not blob_sizes:
        raise QuiltAssemblyError("No blobs provided for quilt assembly.")
    if len(blob_sizes) > n_columns:
        raise QuiltAssemblyError(
            f"Too many blobs for the quilt: {len(blob_sizes) - 1} blobs plus "
            f"the index need {len(blob_sizes)} columns, but the committee's "
            f"shard count provides only {n_columns}."
        )

    min_val: int = max(
        _div_ceil(numerator=sum(blob_sizes), denominator=n_columns * n_rows),
        _div_ceil(
            numerator=blob_sizes[0], denominator=n_rows * max_index_columns
        ),
        _div_ceil(numerator=QUILT_INDEX_PREFIX_SIZE, denominator=n_rows),
    )
    max_val: int = _div_ceil(
        numerator=max(blob_sizes),
        denominator=n_columns // len(blob_sizes) * n_rows,
    )

    while min_val < max_val:
        mid = (min_val + max_val) // 2
        if _can_blobs_fit(
            blob_sizes=blob_sizes, n_columns=n_columns, column_size=mid * n_rows
        ):
            max_val = mid
        else:
            min_val = mid + 1

    symbol_size = (
        _div_ceil(numerator=min_val, denominator=RS2_REQUIRED_ALIGNMENT)
        * RS2_REQUIRED_ALIGNMENT
    )

    if not _can_blobs_fit(
        blob_sizes=blob_sizes,
        n_columns=n_columns,
        column_size=symbol_size * n_rows,
    ):
        raise QuiltAssemblyError(
            "Quilt oversize: the blobs cannot be packed into whole columns "
            f"for a committee of {n_columns} columns."
        )

    if symbol_size > RS2_MAX_SYMBOL_SIZE:
        raise QuiltAssemblyError(
            f"Quilt oversize: the resulting symbol size {symbol_size} exceeds "
            f"the maximum {RS2_MAX_SYMBOL_SIZE}; store fewer or smaller blobs."
        )

    return symbol_size


def _write_blob_to_quilt(
    *,
    quilt: bytearray,
    blob: bytes,
    row_size: int,
    column_size: int,
    symbol_size: int,
    start_column: int,
    prefix: bytes | None = None,
) -> int:
    """Write one blob down consecutive columns and report columns consumed.

    ``prefix`` and ``blob`` are written as ONE contiguous stream, which is
    why the write position is tracked across both rather than restarting:
    a blob's inline metadata and its contents are a single run of bytes that
    happens to be assembled from two buffers.

    Args:
        quilt (bytearray): The matrix being filled, modified in place.
        blob (bytes): The blob's contents.
        row_size (int): Bytes in one row (``symbol_size * n_columns``).
        column_size (int): Bytes in one column (``symbol_size * n_rows``).
        symbol_size (int): Bytes in one symbol.
        start_column (int): First column this blob occupies.
        prefix (bytes | None): Metadata written immediately before ``blob``.

    Returns:
        int: Number of whole columns consumed.

    Raises:
        QuiltAssemblyError: If the matrix dimensions are not whole multiples
            of the symbol size.
    """
    if row_size % symbol_size != 0:
        raise QuiltAssemblyError("Row size must be divisible by symbol size.")
    if column_size % symbol_size != 0:
        raise QuiltAssemblyError("Column size must be divisible by symbol size.")

    n_rows = column_size // symbol_size
    bytes_written = 0

    def write_bytes(data: bytes) -> None:
        """Write one contiguous run into the matrix, column by column.

        Args:
            data (bytes): The run to write, continuing from wherever the
                previous call left off.

        Returns:
            None.

        Raises:
            QuiltAssemblyError: If the run would extend past the matrix.
        """
        nonlocal bytes_written
        offset = bytes_written
        symbols_to_skip = offset // symbol_size
        remaining_offset = offset % symbol_size
        current_col = start_column + symbols_to_skip // n_rows
        current_row = symbols_to_skip % n_rows

        index = 0
        while index < len(data):
            base_index = current_row * row_size + current_col * symbol_size
            start_index = base_index + remaining_offset
            length = min(symbol_size - remaining_offset, len(data) - index)
            if start_index + length > len(quilt):
                # A bytearray slice assignment past the end EXTENDS the
                # buffer rather than raising, where upstream's
                # copy_from_slice panics. Unreachable today, since
                # _can_blobs_fit already guarantees the geometry -- but a
                # silently longer buffer would encode to a valid blob id for
                # the wrong bytes, so this fails pre-spend instead of
                # surfacing later as data nobody can read.
                raise QuiltAssemblyError(
                    f"Quilt write of {length} bytes at offset {start_index} "
                    f"would extend past the {len(quilt)}-byte matrix."
                )
            quilt[start_index : start_index + length] = data[index : index + length]
            index += length
            remaining_offset = 0
            current_row = (current_row + 1) % n_rows
            if current_row == 0:
                current_col += 1

        bytes_written += len(data)

    if prefix is not None:
        write_bytes(prefix)
    write_bytes(blob)

    return math.ceil(bytes_written / column_size)


def validate_quilt_identifier(*, identifier: str) -> None:
    """Reject an identifier upstream's ``validate_quilt_identifier`` would.

    Mirrors that function exactly, and deliberately no more. The Walrus DOCS
    additionally describe an alphanumeric-start rule and a reserved ``_``
    prefix, but the reference implementation checks NEITHER -- enforcing them
    here would reject identifiers that Walrus itself accepts, turning a
    documentation convention into a false failure.

    PRE-SPEND. Assembly runs before registration, so rejecting here costs the
    caller nothing; the alternative is discovering after paying for storage
    that a Rust-based client will not read the quilt back.

    Two comparisons are subtler than they look. The length bound is on UTF-8
    BYTES (Rust's ``str::len()``), not characters, so a non-ASCII identifier
    reaches it sooner than it appears to. And the control-character test is
    Unicode general category ``Cc``, matching Rust's ``char::is_control()``;
    :meth:`str.isprintable` is NOT equivalent, because it also excludes
    separators that are perfectly legal here.

    Args:
        identifier (str): The candidate identifier.

    Raises:
        QuiltAssemblyError: If the identifier is empty, exceeds the byte
            ceiling, carries trailing whitespace, or contains a control
            character.
    """
    if not identifier:
        raise QuiltAssemblyError("Quilt patch identifier must not be empty.")

    encoded_length = len(identifier.encode("utf-8"))
    if encoded_length > MAX_BLOB_IDENTIFIER_BYTES_LENGTH:
        raise QuiltAssemblyError(
            f"Quilt patch identifier is {encoded_length} bytes, over the "
            f"{MAX_BLOB_IDENTIFIER_BYTES_LENGTH}-byte maximum."
        )

    if identifier.rstrip() != identifier:
        raise QuiltAssemblyError(
            f"Quilt patch identifier has trailing whitespace: {identifier!r}"
        )

    if any(unicodedata.category(character) == "Cc" for character in identifier):
        raise QuiltAssemblyError(
            f"Quilt patch identifier contains control characters: {identifier!r}"
        )


def _blob_metadata(*, patch: QuiltPatchInput) -> bytes:
    """Build one packed blob's inline metadata: header, identifier, tags.

    Args:
        patch (QuiltPatchInput): The blob being packed.

    Returns:
        bytes: Header, identifier and (when present) tags, ready to precede
            the blob's contents.

    Raises:
        QuiltAssemblyError: If the BCS-serialized identifier reaches the u16
            length field's ceiling, or the payload exceeds the u32 one.
    """
    # validate_quilt_identifier bounds the RAW UTF-8 length, mirroring
    # upstream's construction-time check. Upstream ALSO bounds the
    # BCS-SERIALIZED length at serialization time (serialized_blob_size,
    # quilt_encoding.rs:728) -- and that is what this 2-byte prefix actually
    # carries, since the ULEB128 prefix makes it 1-3 bytes longer than the
    # raw length. Both checks are needed to accept exactly the identifiers
    # upstream accepts; without this one an oversize identifier escapes as
    # OverflowError out of to_bytes() rather than as QuiltAssemblyError.
    identifier_bytes = bcse.String.encode(patch.identifier)
    if len(identifier_bytes) >= MAX_BLOB_IDENTIFIER_BYTES_LENGTH:
        raise QuiltAssemblyError(
            f"Quilt patch identifier {patch.identifier!r} serializes to "
            f"{len(identifier_bytes)} bytes, at or above the "
            f"{MAX_BLOB_IDENTIFIER_BYTES_LENGTH}-byte ceiling upstream "
            "enforces on the u16 identifier length field."
        )

    tag_bytes = _serialize_tags(tags=patch.tags) if patch.tags else None

    metadata_size = (
        QUILT_PATCH_BLOB_HEADER_SIZE
        + BLOB_IDENTIFIER_SIZE_BYTES_LENGTH
        + len(identifier_bytes)
    )
    mask = 0
    if tag_bytes is not None:
        metadata_size += TAGS_SIZE_BYTES_LENGTH + len(tag_bytes)
        mask |= HAS_TAGS_FLAG

    # The header's length field covers everything AFTER the header itself --
    # the identifier and tag sections plus the blob's own contents.
    payload_length = metadata_size - QUILT_PATCH_BLOB_HEADER_SIZE + len(patch.contents)

    if payload_length > MAX_SERIALIZED_BLOB_SIZE:
        raise QuiltAssemblyError(
            f"Quilt patch {patch.identifier!r} serializes to {payload_length} "
            f"bytes, over the {MAX_SERIALIZED_BLOB_SIZE}-byte maximum the u32 "
            "header length field can carry; store it as its own blob."
        )

    buffer = bytearray()
    buffer.append(QUILT_VERSION_BYTE)
    buffer += payload_length.to_bytes(4, "little")
    buffer.append(mask)
    buffer += len(identifier_bytes).to_bytes(2, "little")
    buffer += identifier_bytes
    if tag_bytes is not None:
        buffer += len(tag_bytes).to_bytes(2, "little")
        buffer += tag_bytes

    return bytes(buffer)


def assemble_quilt(
    *, patches: typing.Sequence[QuiltPatchInput], n_shards: int
) -> AssembledQuilt:
    """Pack blobs into one quilt buffer for a committee's shard count.

    The result is an ORDINARY Walrus blob: encode, register, upload and
    certify it exactly as any other. Nothing downstream needs to know it is
    a quilt.

    PATCHES ARE SORTED BY IDENTIFIER, not kept in the caller's order. The
    packing order determines every patch's column range, and therefore its
    ``QuiltPatchId``, so it has to be a function of the batch's content
    rather than of how a caller happened to list it.

    Args:
        patches (typing.Sequence[QuiltPatchInput]): Blobs to pack. Must be
            non-empty with unique identifiers.
        n_shards (int): The committee's shard count, which fixes the matrix
            geometry. The SAME value must be used to encode the result.

    Returns:
        AssembledQuilt: The buffer, each patch's column range, and the shard
            count the geometry was built for.

    Raises:
        QuiltAssemblyError: If no patches are given, identifiers collide or
            are oversize, or the batch cannot be packed for this shard count.
    """
    if not patches:
        raise QuiltAssemblyError("A quilt needs at least one blob.")

    n_rows, n_columns = source_symbol_counts(n_shards=n_shards)

    ordered = sorted(patches, key=lambda patch: patch.identifier)
    seen: set[str] = set()
    for patch in ordered:
        validate_quilt_identifier(identifier=patch.identifier)
        if patch.identifier in seen:
            raise QuiltAssemblyError(
                f"Duplicate quilt patch identifier: {patch.identifier!r}. "
                "Identifiers must be unique within a quilt."
            )
        seen.add(patch.identifier)

    metadata = [_blob_metadata(patch=patch) for patch in ordered]

    # Sizing the index needs the index, which needs every patch's column
    # range -- which is not known yet. It resolves because end_index is a
    # FIXED-WIDTH u16: placeholder zeros serialize to exactly as many bytes
    # as the real values will, so the size is exact even though the values
    # are not.
    placeholder = QuiltIndexV1(
        quilt_patches=[
            QuiltPatchV1(
                end_index=0,
                identifier=patch.identifier,
                tags=sorted_tag_entries(tags=patch.tags),
            )
            for patch in ordered
        ]
    )
    index_size = QUILT_INDEX_PREFIX_SIZE + len(placeholder.serialize())

    blob_sizes = [index_size] + [
        len(meta) + len(patch.contents) for meta, patch in zip(metadata, ordered)
    ]

    symbol_size = compute_symbol_size(
        blob_sizes=blob_sizes, n_columns=n_columns, n_rows=n_rows
    )
    row_size = symbol_size * n_columns
    column_size = symbol_size * n_rows

    index_columns = math.ceil(index_size / column_size)
    if index_columns > MAX_NUM_SLIVERS_FOR_QUILT_INDEX:
        raise QuiltAssemblyError(
            f"The quilt index needs {index_columns} columns, over the "
            f"{MAX_NUM_SLIVERS_FOR_QUILT_INDEX}-column maximum; store fewer blobs."
        )

    quilt = bytearray(row_size * n_rows)

    current_column = index_columns
    layouts: list[QuiltPatchLayout] = []
    for patch, meta in zip(ordered, metadata):
        start_column = current_column
        current_column += _write_blob_to_quilt(
            quilt=quilt,
            blob=patch.contents,
            row_size=row_size,
            column_size=column_size,
            symbol_size=symbol_size,
            start_column=current_column,
            prefix=meta,
        )
        layouts.append(
            QuiltPatchLayout(
                identifier=patch.identifier,
                tags=dict(patch.tags),
                start_index=start_column,
                end_index=current_column,
            )
        )

    index_bytes = QuiltIndexV1(
        quilt_patches=[QuiltPatchV1.from_layout(layout=layout) for layout in layouts]
    ).serialize()

    index_blob = bytearray()
    index_blob.append(QUILT_VERSION_BYTE)
    index_blob += len(index_bytes).to_bytes(4, "little")
    index_blob += index_bytes

    # The column budget above was reserved from the PLACEHOLDER index's size.
    # If the real index were larger it would overwrite the first patch's
    # columns and produce a quilt that reads back as corruption, so the
    # fixed-width assumption is checked rather than trusted.
    if len(index_blob) != index_size:
        raise QuiltAssemblyError(
            f"Quilt index size changed between sizing ({index_size} bytes) and "
            f"serialization ({len(index_blob)} bytes)."
        )

    _write_blob_to_quilt(
        quilt=quilt,
        blob=bytes(index_blob),
        row_size=row_size,
        column_size=column_size,
        symbol_size=symbol_size,
        start_column=0,
    )

    return AssembledQuilt(
        data=bytes(quilt), patches=tuple(layouts), n_shards=n_shards
    )


def quilt_patch_id(*, quilt_id: bytes, layout: QuiltPatchLayout) -> str:
    """Compose a patch's ``QuiltPatchId`` as URL-safe unpadded base64.

    37 bytes: the 32-byte quilt blob ID, a version byte, then the patch's
    ``start_index`` and ``end_index`` as u16 little-endian. The quilt ID is
    the blob ID of the ASSEMBLED quilt, so this cannot be composed until the
    buffer has been encoded -- a patch ID is not derivable from the patch's
    own contents.

    The base64 is written here rather than through
    :func:`~pytusk.core.encoding.redstuff.blob_id_to_url_base64`, whose
    contract is specifically a 32-byte BLOB id; a 37-byte patch id is a
    different thing that happens to share an alphabet.

    Args:
        quilt_id (bytes): The assembled quilt's 32-byte blob ID.
        layout (QuiltPatchLayout): The patch's column range.

    Returns:
        str: The patch ID, URL-safe base64 with padding stripped.

    Raises:
        ValueError: If ``quilt_id`` is not exactly 32 bytes.
    """
    if len(quilt_id) != 32:
        raise ValueError(f"quilt_id must be 32 bytes, got {len(quilt_id)}")
    raw = (
        quilt_id
        + bytes([QUILT_VERSION_BYTE])
        + layout.start_index.to_bytes(2, "little")
        + layout.end_index.to_bytes(2, "little")
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
