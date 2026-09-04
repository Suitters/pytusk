#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for Walrus event -> Blob object ID resolution.

THE INPUT BELOW IS A REAL WIRE CAPTURE, NOT A RECONSTRUCTION. That is the
whole point of this file. An earlier version of the decoder was tested
against a payload hand-built from a documented field table; it passed, and
was still wrong, because it decoded ``contents`` as a bare Move payload
when the wire actually carries Sui's BCS ``Event`` envelope wrapped around
it. Every live lookup returned None. A test built from the same assumption
as the code cannot catch that class of error -- only real bytes can.
"""

import pytest

from pytusk.core.chain.events import _EVENT_LAYOUTS, event_object_id

# Captured 2026-09-01 from testnet transaction
# DaRmjvMBZoU6cU1iZUM3qMdakFYa5jJcmWFyaxW2aReN, event 0 -- the certify
# transaction for blob lATLPe9-w0AkcvWAtDThx49IlvNrn8GLWkh2tv9pXGw.
# 200 bytes: BCS Event envelope (126 bytes) + 74-byte BlobCertified payload.
_LIVE_BLOB_CERTIFIED = bytes.fromhex(
    "849e95d2718938d66c37fb91df76d72f78526c1864c339bac415ce8ecda2d8cc"
    "0673797374656d"
    "0a45e1f06dc056f402048e18ed26e074d1cebdac889c628a3e042d8b1017a54a"
    "d84704c17fc870b8764832c535aa6b11f21a95cd6f5bb38a9b07d2cf42220c66"
    "066576656e74730d426c6f6243657274696669656400"
    "4a"
    "fb0100009404cb3def7ec3402472f580b434e1c78f4896f36b9fc18b5a4876b6"
    "ff695c6cfc010000007ad6fc95a7b557cc4bae10040f46ac85f974bb93057591"
    "4b9036dad2346a826900"
)

_LIVE_EVENT_TYPE = (
    "0xd84704c17fc870b8764832c535aa6b11f21a95cd6f5bb38a9b07d2cf42220c66"
    "::events::BlobCertified"
)

_LIVE_OBJECT_ID = "0x7ad6fc95a7b557cc4bae10040f46ac85f974bb930575914b9036dad2346a8269"


class TestLiveWireCapture:
    """The decoder against bytes that actually came off the network."""

    def test_capture_is_the_expected_length(self) -> None:
        """Guards the fixture itself: 126-byte envelope + 74-byte payload."""
        assert len(_LIVE_BLOB_CERTIFIED) == 200

    def test_recovers_the_object_id(self) -> None:
        """The whole point: real bytes in, correct Blob object ID out."""
        assert (
            event_object_id(
                event_type=_LIVE_EVENT_TYPE, contents=_LIVE_BLOB_CERTIFIED
            )
            == _LIVE_OBJECT_ID
        )

    def test_package_in_envelope_differs_from_defining_package(self) -> None:
        """The envelope's first 32 bytes are the CALLING package.

        Pinned because it is genuinely surprising: the event type names
        0xd84704c1..., while the envelope opens with 0x849e95d2.... Code
        that assumed those were the same would mis-locate every field.
        """
        assert _LIVE_BLOB_CERTIFIED[:32].hex() != _LIVE_EVENT_TYPE[2:66]


class TestMalformedInputIsRefused:
    """A misparse must yield nothing, never a plausible wrong object ID."""

    @pytest.mark.parametrize(
        "label,contents",
        [
            ("empty", b""),
            ("truncated", _LIVE_BLOB_CERTIFIED[:-1]),
            ("one byte over", _LIVE_BLOB_CERTIFIED + b"\x00"),
            ("envelope only", _LIVE_BLOB_CERTIFIED[:126]),
            ("header fragment", _LIVE_BLOB_CERTIFIED[:40]),
        ],
    )
    def test_returns_none(self, label: str, contents: bytes) -> None:
        """Every malformed shape returns None rather than guessing."""
        assert (
            event_object_id(event_type=_LIVE_EVENT_TYPE, contents=contents) is None
        )

    def test_unknown_event_type_returns_none(self) -> None:
        """An event this module has no layout for is not decoded."""
        assert (
            event_object_id(
                event_type="0xa::events::SomethingElse",
                contents=_LIVE_BLOB_CERTIFIED,
            )
            is None
        )

    def test_payload_must_end_exactly_at_buffer_end(self) -> None:
        """A trailing byte means the envelope was misparsed.

        Without this check a shifted parse could still read 32 bytes and
        return a well-formed -- but wrong -- object ID.
        """
        assert (
            event_object_id(
                event_type=_LIVE_EVENT_TYPE,
                contents=_LIVE_BLOB_CERTIFIED + b"\xff",
            )
            is None
        )


class TestLayoutTable:
    """The declared Move layouts, transcribed from events.move."""

    def test_blob_certified_total_matches_live_payload(self) -> None:
        """74 bytes, which is what the live ULEB prefix (0x4a) announced."""
        total = sum(size for _, size in _EVENT_LAYOUTS["BlobCertified"])
        assert total == 74
        assert _LIVE_BLOB_CERTIFIED[125] == 0x4A

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("BlobRegistered", 82),
            ("BlobCertified", 74),
            ("BlobDeleted", 73),
            ("InvalidBlobID", 36),
        ],
    )
    def test_layout_totals(self, name: str, expected: int) -> None:
        """Each struct's fixed-width total, from its Move declaration."""
        assert sum(size for _, size in _EVENT_LAYOUTS[name]) == expected

    def test_invalid_blob_id_declares_no_object_id(self) -> None:
        """An invalidated blob names no Blob object -- a true answer, not a gap."""
        fields = [name for name, _ in _EVENT_LAYOUTS["InvalidBlobID"]]
        assert "object_id" not in fields

    @pytest.mark.parametrize(
        "name", ["BlobRegistered", "BlobCertified", "BlobDeleted"]
    )
    def test_object_id_is_32_bytes(self, name: str) -> None:
        """Sui IDs are address-shaped: 32 raw bytes, no length prefix."""
        sizes = dict(_EVENT_LAYOUTS[name])
        assert sizes["object_id"] == 32
