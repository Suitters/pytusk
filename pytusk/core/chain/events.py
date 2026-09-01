#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Resolve a Walrus blob-lifecycle event to the ``Blob`` object it names.

A storage node reports the event that established a blob's status as an
``(tx_digest, event_seq)`` pair. Turning that into the ``Blob`` object ID is
what lets a caller enrich a status verdict with lease facts for a blob it
does not own -- Sui objects are publicly readable, so ownership gates
nothing here.

WHY THIS DECODES BCS RATHER THAN READING FIELDS. ``Event.json`` is UNSET
when an event is carried inside its own transaction's ``events`` list -- the
enclosing ``ExecutedTransaction`` already supplies that context, the same
rule its ``checkpoint`` field documents. Verified 2026-09-01 against a live
certify transaction, whose ``BlobCertified`` event carried only
``packageId``, ``module``, ``sender``, ``eventType`` and ``contents``. The
object ID therefore exists ONLY inside the BCS-encoded ``contents``, so this
module decodes it.

For the same reason an event must NOT be located by matching
``Event.transaction_digest`` against the reference's digest: that field is
also unset on this path. The transaction is fetched by digest and the event
selected POSITIONALLY, which is exactly what an ``EventID``'s sequence
number means.

``contents`` is NOT the bare Move payload. It is the BCS encoding of Sui's
own ``Event`` envelope, which wraps the payload in five preceding fields:

    package_id: 32 bytes (the CALLING package, which differs from the
                package that DEFINES the event type)
    transaction_module: ULEB-length string
    sender: 32 bytes
    type_: StructTag -- 32-byte address, ULEB string module, ULEB string
           name, ULEB-count type-parameter vector
    contents: ULEB length, then the Move struct payload

Verified against a live 200-byte ``BlobCertified`` event whose 74-byte
payload begins at offset 126. An earlier version of this module decoded
``contents`` as if it were the payload with only a length prefix -- that
figure came from the ``wallet txns txn`` rendering, which shows the payload
alone, and it made every lookup return None against real wire bytes.

BCS carries no field names and no per-field framing for fixed-width types,
so a decode is entirely positional: read the wrong layout and a neighbouring
field's bytes silently become an object ID. Each layout below is therefore
declared field-by-field from the Move source and the total is CHECKED
against the payload length before any field is read, so a contract change
fails loudly instead of returning a plausible wrong ID.
"""

import logging

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import SuiRpcResult
from pysui.sui.sui_common.sui_commands import GetTransaction

from pytusk.core.chain.committee import ChainReader

_logger = logging.getLogger(__name__)

__all__ = [
    "event_object_id",
    "fetch_event_object_id",
]

# BCS wire sizes for the primitives these events use. `ID` is address-shaped
# and encodes as 32 raw bytes with NO length prefix; `u256` likewise.
_U8 = 1
_BOOL = 1
_U32 = 4
_U64 = 8
_U256 = 32
_ID = 32

# Field layouts transcribed from the Move source, in DECLARATION ORDER,
# which is the only order BCS encodes:
#   ~/mysten_repos/walrus/contracts/walrus/sources/system/events.move
# None of these structs contain an Option or a vector, so every field is
# fixed-width and the total size is exact.
_EVENT_LAYOUTS: dict[str, tuple[tuple[str, int], ...]] = {
    # events.move:13-22
    "BlobRegistered": (
        ("epoch", _U32),
        ("blob_id", _U256),
        ("size", _U64),
        ("encoding_type", _U8),
        ("end_epoch", _U32),
        ("deletable", _BOOL),
        ("object_id", _ID),
    ),
    # events.move:25-33 -- layout independently confirmed against a live
    # testnet certify transaction: 74 payload bytes behind a 0x4a ULEB
    # prefix, matching this declaration exactly.
    "BlobCertified": (
        ("epoch", _U32),
        ("blob_id", _U256),
        ("end_epoch", _U32),
        ("deletable", _BOOL),
        ("object_id", _ID),
        ("is_extension", _BOOL),
    ),
    # events.move:37-44
    "BlobDeleted": (
        ("epoch", _U32),
        ("blob_id", _U256),
        ("end_epoch", _U32),
        ("object_id", _ID),
        ("was_certified", _BOOL),
    ),
    # events.move:49-52 -- deliberately present with NO object_id. An
    # invalidated blob names no Blob object, so a caller asking for one
    # gets None rather than an error: that is a true answer, not a
    # failure.
    "InvalidBlobID": (
        ("epoch", _U32),
        ("blob_id", _U256),
    ),
}


def _read_uleb(*, raw: bytes, offset: int) -> tuple[int, int] | None:
    """Read a ULEB128 integer.

    Args:
        raw (bytes): Buffer to read from.
        offset (int): Byte offset to start at.

    Returns:
        tuple[int, int] | None: (value, next_offset), or None if the
            encoding is malformed or runs off the end of the buffer.
    """
    value = 0
    shift = 0
    index = offset
    while index < len(raw):
        byte = raw[index]
        value |= (byte & 0x7F) << shift
        index += 1
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 35:
            return None
    return None


def _skip_uleb_bytes(*, raw: bytes, offset: int) -> int | None:
    """Skip a ULEB-length-prefixed byte run (a BCS string or vector).

    Args:
        raw (bytes): Buffer to read from.
        offset (int): Offset of the length prefix.

    Returns:
        int | None: Offset just past the run, or None if malformed.
    """
    read = _read_uleb(raw=raw, offset=offset)
    if read is None:
        return None
    length, next_offset = read
    end = next_offset + length
    return end if end <= len(raw) else None


def _event_payload(*, raw: bytes) -> bytes | None:
    """Unwrap Sui's BCS ``Event`` envelope and return the Move payload.

    See this module's docstring for the envelope's field order. Every step
    is bounds-checked; any malformed hop returns None rather than reading
    past the buffer or producing a shifted payload.

    Args:
        raw (bytes): The raw ``Event.contents.value`` bytes.

    Returns:
        bytes | None: The Move struct payload, or None if the envelope
            does not parse or does not consume the buffer exactly.
    """
    offset = 32  # package_id
    if offset > len(raw):
        return None
    skipped = _skip_uleb_bytes(raw=raw, offset=offset)  # transaction_module
    if skipped is None:
        return None
    offset = skipped + 32 + 32  # sender, then StructTag.address
    if offset > len(raw):
        return None
    for _ in range(2):  # StructTag module, then name
        skipped = _skip_uleb_bytes(raw=raw, offset=offset)
        if skipped is None:
            return None
        offset = skipped

    read = _read_uleb(raw=raw, offset=offset)  # type-parameter count
    if read is None:
        return None
    type_param_count, offset = read
    if type_param_count != 0:
        # A generic event would need full type-tag parsing to find where
        # the parameters end. None of the Walrus blob events are generic,
        # so refuse rather than guess at a length.
        return None

    read = _read_uleb(raw=raw, offset=offset)  # payload length
    if read is None:
        return None
    length, offset = read
    # The payload must run to exactly the end of the buffer. Anything else
    # means the envelope was misparsed, and a shifted payload would decode
    # a neighbouring field as an object ID.
    if offset + length != len(raw):
        return None
    return raw[offset : offset + length]


def _struct_name(*, event_type: str) -> str:
    """Reduce a fully-qualified event type to its struct name.

    The package address varies by deployment, so only the trailing
    ``::events::<Name>`` portion is stable and matchable.

    Args:
        event_type (str): e.g. ``"0xabc::events::BlobCertified"``.

    Returns:
        str: The struct name, or the input unchanged if it has no ``::``.
    """
    return event_type.rsplit("::", 1)[-1]


def event_object_id(*, event_type: str, contents: bytes) -> str | None:
    """Decode a Walrus event payload and return the ``Blob`` object ID.

    Client-free and pure: this is the half of the accessor that a caller
    holding an already-fetched event can use directly.

    Args:
        event_type (str): Fully-qualified Move event type.
        contents (bytes): Raw ``Event.contents.value`` -- the BCS ``Event``
            envelope, NOT the bare Move payload.

    Returns:
        str | None: ``0x``-prefixed object ID, or None when this event type
            carries no object ID (``InvalidBlobID``), is not a Walrus blob
            event, or does not decode against its declared layout. The last
            case is a DEFECT rather than an outcome and is logged at
            WARNING; the others are silent.
    """
    struct_name = _struct_name(event_type=event_type)
    layout = _EVENT_LAYOUTS.get(struct_name)
    if layout is None:
        return None

    payload = _event_payload(raw=contents)
    if payload is None:
        return None

    # Length check BEFORE reading any field. A mismatch means the contract
    # changed shape; decoding anyway would return a neighbouring field's
    # bytes as an object ID, which is worse than returning nothing.
    #
    # This one case is logged, and the other None returns are not, because
    # it differs IN KIND from them: a pruned transaction or an event with
    # no object ID is an ordinary outcome, whereas a payload that does not
    # match its declared layout is a defect. Without this line an on-chain
    # field reordering would silently degrade every caller to its fallback
    # path, looking exactly like ordinary absence.
    expected = sum(size for _, size in layout)
    if len(payload) != expected:
        _logger.warning(
            "Walrus event layout mismatch for %s: expected %d payload bytes, "
            "got %d. The on-chain contract may have changed shape; object-ID "
            "resolution is degraded until the layout declared in %s is "
            "updated.",
            struct_name,
            expected,
            len(payload),
            __name__,
        )
        return None

    offset = 0
    for name, size in layout:
        if name == "object_id":
            return "0x" + payload[offset : offset + size].hex()
        offset += size
    return None


async def fetch_event_object_id(
    *, reader: ChainReader, tx_digest: str, event_seq: int
) -> str | None:
    """Resolve an ``(tx_digest, event_seq)`` reference to a ``Blob`` object ID.

    Returns None rather than raising for every "could not resolve" case.
    This is ENRICHMENT: the caller falls back to a thinner answer when the
    object cannot be reached, and a pruned or unindexed transaction is an
    ordinary outcome, not a defect. That is deliberately unlike
    :mod:`pytusk.core.chain.blob_fields`, which raises on a missing field --
    there, the caller already holds an object it knows exists, so absence
    means the read was incomplete.

    Args:
        reader (ChainReader): Sui transport.
        tx_digest (str): Base58 digest of the transaction carrying the event.
        event_seq (int): Zero-based index of the event within that
            transaction.

    Returns:
        str | None: ``0x``-prefixed object ID, or None if the transaction,
            the event, or the object ID could not be resolved.
    """
    result: SuiRpcResult = await reader.execute(command=GetTransaction(digest=tx_digest))
    if not result.is_ok():
        return None
    executed: sui_prot.ExecutedTransaction | None = result.result_data
    if executed is None or executed.events is None:
        return None

    events = executed.events.events
    if event_seq < 0 or event_seq >= len(events):
        return None
    event = events[event_seq]
    if event.event_type is None or event.contents is None:
        return None
    if event.contents.value is None:
        return None

    return event_object_id(
        event_type=event.event_type, contents=event.contents.value
    )
