#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Input and layout types for locally assembled Walrus quilts.

A quilt batches many small blobs into ONE Walrus blob, so the fixed per-blob
overhead -- metadata, Sui registration, the erasure-coding floor -- is paid
once for the batch instead of once per file. pytusk assembles that batch
locally, which is what makes a quilt writable through the upload relay, and
therefore on mainnet, where no public publisher exists.

THREE TYPES, THREE DIFFERENT MOMENTS. They are deliberately not one type:

- :class:`QuiltPatchInput` is what a CALLER supplies -- an identifier, the
  bytes, and optional tags. It knows nothing about where the blob will sit.
- :class:`QuiltPatchLayout` is what ASSEMBLY decides -- the same identity
  plus the half-open column range the patch occupies in the quilt matrix.
  Those columns exist only once the whole batch has been sorted and packed.
- :class:`AssembledQuilt` is the finished byte buffer plus every layout.

A patch's ``QuiltPatchId`` is deliberately absent from all three. It is
``quilt_id ++ version ++ start_index ++ end_index``, and ``quilt_id`` is the
blob ID of the ASSEMBLED quilt -- which does not exist until the buffer here
has been RedStuff-encoded. Carrying an Optional patch-id that is unset for
the whole of assembly and set afterwards would hide that ordering, so the id
is composed separately once encoding has produced a blob ID.

TAG ORDERING IS PROTOCOL-VISIBLE. Upstream types tags as a Rust
``BTreeMap``, chosen (per its own source comment) "to ensure deterministic
serialization", so the wire bytes carry entries sorted by key. ``tags`` here
is an ordinary ``dict`` because that is what a caller wants to write; the
sort is applied at SERIALIZATION, not stored here. A caller's insertion
order never means anything.
"""

import dataclasses


@dataclasses.dataclass(kw_only=True, frozen=True)
class QuiltPatchInput:
    """One blob a caller wants placed into a quilt.

    Attributes:
        identifier (str): Name used to locate this blob inside the quilt.
            Must be non-empty, must not carry trailing whitespace, must
            contain no control characters, and must be unique within the
            quilt. Its 65535 ceiling is a BYTE count, not a character count
            -- a non-ASCII identifier consumes more of it than its length
            suggests. These mirror upstream's ``validate_quilt_identifier``
            exactly and are checked during assembly, which is PRE-SPEND, so
            a violation raises. The Walrus DOCS additionally describe an
            alphanumeric-start rule and a reserved ``_`` prefix; the
            reference implementation enforces NEITHER, so neither is
            enforced here.
        contents (bytes): The blob's raw, unencoded bytes.
        tags (dict[str, str]): Optional key/value metadata stored alongside
            the blob inside the quilt and usable for lookup. Serialized
            key-sorted regardless of the order given here -- see this
            module's note on tag ordering.
    """

    identifier: str
    contents: bytes
    tags: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(kw_only=True, frozen=True)
class QuiltPatchLayout:
    """Where one patch ended up in the assembled quilt matrix.

    Produced by assembly, never supplied by a caller: the column range
    depends on the whole batch -- its sort order, every sibling's size, and
    the shard-count-derived matrix geometry -- so it cannot be known for a
    blob in isolation.

    Attributes:
        identifier (str): Carried through from the corresponding
            :class:`QuiltPatchInput`.
        tags (dict[str, str]): Carried through unchanged.
        start_index (int): First matrix column this patch occupies. DERIVED,
            and deliberately NOT part of the serialized quilt index --
            upstream marks it ``#[serde(skip)]`` and rebuilds it on read by
            chaining each patch's ``end_index``. It is kept here because
            composing the patch's ``QuiltPatchId`` needs it.
        end_index (int): One PAST the last column this patch occupies. The
            range is half-open, matching upstream's ``start..end``.
    """

    identifier: str
    tags: dict[str, str]
    start_index: int
    end_index: int


@dataclasses.dataclass(kw_only=True, frozen=True)
class QuiltPatchReceipt(QuiltPatchLayout):
    """A patch's layout plus the ``QuiltPatchId`` that addresses it.

    The id cannot exist at assembly time: it derives from the quilt's blob
    id, which is only known after encoding. Adding it in a subclass rather
    than widening :class:`QuiltPatchLayout` keeps that type honest -- a
    ``patch_id`` field on the layout would sit unset for half the type's
    life, which is the optional-that-only-means-something-elsewhere shape
    this codebase splits rather than tolerates.

    Attributes:
        patch_id (str): The patch's ``QuiltPatchId`` as URL-safe unpadded
            base64. Callers treat it as OPAQUE -- the aggregator parses it
            server-side (``GET /v1/blobs/by-quilt-patch-id/<PATCH_ID>``), so
            nothing on this side needs to decode it back.
    """

    patch_id: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class AssembledQuilt:
    """A finished quilt: the bytes to store, and where each patch landed.

    ``data`` is an ORDINARY Walrus blob from this point on. Nothing
    downstream -- RedStuff encoding, registration, relay upload,
    certification -- treats it differently from any other byte payload. The
    only things recording that it is a quilt are an on-chain blob attribute
    set during registration and a version byte inside these bytes.

    Attributes:
        data (bytes): The assembled quilt buffer, ready to be encoded and
            stored exactly like a single blob.
        patches (tuple[QuiltPatchLayout, ...]): One layout per input blob,
            in the order they were packed -- sorted by identifier, NOT the
            caller's input order.
        n_shards (int): Shard count the matrix geometry was built for. The
            layout is only valid against this value, because the column
            count derives from it: bytes assembled for one committee size do
            not describe the same quilt under another. Recorded so that a
            later encode can be checked against it rather than silently
            producing a malformed quilt.
    """

    data: bytes
    patches: tuple[QuiltPatchLayout, ...]
    n_shards: int
