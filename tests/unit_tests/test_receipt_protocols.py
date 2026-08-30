#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests proving :class:`~pytusk.core.types.protocols.UploadReceipt`
and :class:`~pytusk.core.types.protocols.StageTimingsProtocol` are
satisfied structurally by :class:`~pytusk.core.types.receipts.NativeBlobReceipt`,
:class:`~pytusk.core.types.receipts.RelayBlobReceipt`,
:class:`~pytusk.core.types.receipts.QuiltRelayReceipt`,
:class:`~pytusk.core.types.receipts.StageTimings`, and
:class:`~pytusk.core.types.receipts.RelayStageTimings`, with no change to
any of those five dataclasses.

Conformance is proven two ways:

- STATICALLY, via module-level identity-returning functions whose
  parameter type is the concrete dataclass and whose return type is the
  protocol -- a type checker (mypy/pyright) rejects this file if the
  concrete type does not structurally satisfy the protocol. This is the
  load-bearing check: it is what would catch a future edit that
  accidentally narrows a protocol member back into an invariant bare
  attribute.
- AT RUNTIME, by constructing one instance of each concrete type and
  reading every protocol-declared member off it, plus an ``isinstance``
  check against the ``@runtime_checkable`` protocols.
"""

import pytest

from pytusk.core.types.outcomes import RelayOutcome
from pytusk.core.types.protocols import StageTimingsProtocol, UploadReceipt
from pytusk.core.types.quilts import QuiltPatchReceipt
from pytusk.core.types.receipts import (
    NativeBlobReceipt,
    QuiltRelayReceipt,
    RelayBlobReceipt,
    RelayStageTimings,
    StageTimings,
)

# --- Static conformance -----------------------------------------------
#
# These functions are never called; their existence is the assertion.
# A type checker validates that each concrete dataclass structurally
# satisfies the declared protocol return type.


def _assert_native_receipt_conforms(receipt: NativeBlobReceipt) -> UploadReceipt:
    """Static proof: ``NativeBlobReceipt`` satisfies ``UploadReceipt``."""
    return receipt


def _assert_relay_receipt_conforms(receipt: RelayBlobReceipt) -> UploadReceipt:
    """Static proof: ``RelayBlobReceipt`` satisfies ``UploadReceipt``."""
    return receipt


def _assert_quilt_relay_receipt_conforms(
    receipt: QuiltRelayReceipt,
) -> UploadReceipt:
    """Static proof: ``QuiltRelayReceipt`` satisfies ``UploadReceipt``.

    The subclass ADDS a field; it must never narrow anything the protocol
    declares, or a quilt receipt stops being usable everywhere a blob
    receipt is -- which is the whole reason it subclasses rather than
    standing alone.
    """
    return receipt


def _assert_native_timings_conform(
    timings: StageTimings,
) -> StageTimingsProtocol:
    """Static proof: ``StageTimings`` satisfies ``StageTimingsProtocol``."""
    return timings


def _assert_relay_timings_conform(
    timings: RelayStageTimings,
) -> StageTimingsProtocol:
    """Static proof: ``RelayStageTimings`` satisfies ``StageTimingsProtocol``."""
    return timings


# --- Runtime conformance -------------------------------------------------


def _native_timings() -> StageTimings:
    return StageTimings(
        encode=0.1,
        register_tx1=0.2,
        sliver_upload=0.3,
        confirmations=0.4,
        certify_tx2=0.5,
        total=1.5,
    )


def _relay_timings() -> RelayStageTimings:
    return RelayStageTimings(
        encode=0.1,
        tip_config=0.2,
        register_tip_tx=0.3,
        relay_upload=0.4,
        certify_tx=0.5,
        total=1.5,
    )


def _native_receipt() -> NativeBlobReceipt:
    return NativeBlobReceipt(
        blob_id="blob-id",
        object_id="object-id",
        certified=True,
        end_epoch=42,
        failed_stage=None,
        timings=_native_timings(),
    )


def _relay_receipt() -> RelayBlobReceipt:
    return RelayBlobReceipt(
        outcome=RelayOutcome.CERTIFIED,
        blob_id="blob-id",
        object_id="object-id",
        certified=True,
        end_epoch=42,
        failed_stage=None,
        timings=_relay_timings(),
        relay_url="https://relay.example",
        tip_paid=True,
        tip_amount=100,
        register_tx_digest="digest-1",
        certify_tx_digest="digest-2",
        nonce=None,
        relay_status=None,
        relay_message=None,
        attempts=1,
        transport_error=None,
    )


def _quilt_patch_receipt() -> QuiltPatchReceipt:
    return QuiltPatchReceipt(
        identifier="a.bin",
        tags={"kind": "test"},
        start_index=0,
        end_index=3,
        patch_id="patch-id",
    )


def _quilt_relay_receipt() -> QuiltRelayReceipt:
    return QuiltRelayReceipt(
        outcome=RelayOutcome.CERTIFIED,
        blob_id="blob-id",
        object_id="object-id",
        certified=True,
        end_epoch=42,
        failed_stage=None,
        timings=_relay_timings(),
        relay_url="https://relay.example",
        tip_paid=True,
        tip_amount=100,
        register_tx_digest="digest-1",
        certify_tx_digest="digest-2",
        nonce=None,
        relay_status=None,
        relay_message=None,
        attempts=1,
        transport_error=None,
        patches=(_quilt_patch_receipt(),),
    )


class TestStageTimingsProtocolRuntime:
    """Runtime readability and ``isinstance`` checks for
    :class:`StageTimingsProtocol`.
    """

    def test_native_timings_members_readable(self) -> None:
        timings = _native_timings()
        assert timings.encode == 0.1
        assert timings.total == 1.5

    def test_relay_timings_members_readable(self) -> None:
        timings = _relay_timings()
        assert timings.encode == 0.1
        assert timings.total == 1.5

    def test_native_timings_isinstance(self) -> None:
        assert isinstance(_native_timings(), StageTimingsProtocol)

    def test_relay_timings_isinstance(self) -> None:
        assert isinstance(_relay_timings(), StageTimingsProtocol)


class TestUploadReceiptProtocolRuntime:
    """Runtime readability and ``isinstance`` checks for
    :class:`UploadReceipt`.
    """

    def test_native_receipt_members_readable(self) -> None:
        receipt = _native_receipt()
        assert receipt.certified is True
        assert receipt.failed_stage is None
        assert receipt.blob_id == "blob-id"
        assert receipt.object_id == "object-id"
        assert receipt.end_epoch == 42
        assert receipt.timings.encode == 0.1
        assert receipt.timings.total == 1.5

    def test_relay_receipt_members_readable(self) -> None:
        receipt = _relay_receipt()
        assert receipt.certified is True
        assert receipt.failed_stage is None
        assert receipt.blob_id == "blob-id"
        assert receipt.object_id == "object-id"
        assert receipt.end_epoch == 42
        assert receipt.timings.encode == 0.1
        assert receipt.timings.total == 1.5

    def test_native_receipt_isinstance(self) -> None:
        assert isinstance(_native_receipt(), UploadReceipt)

    def test_relay_receipt_isinstance(self) -> None:
        assert isinstance(_relay_receipt(), UploadReceipt)

    def test_quilt_relay_receipt_members_readable(self) -> None:
        receipt = _quilt_relay_receipt()
        assert receipt.certified is True
        assert receipt.failed_stage is None
        assert receipt.blob_id == "blob-id"
        assert receipt.object_id == "object-id"
        assert receipt.end_epoch == 42
        assert receipt.timings.encode == 0.1
        assert receipt.timings.total == 1.5

    def test_quilt_relay_receipt_isinstance(self) -> None:
        assert isinstance(_quilt_relay_receipt(), UploadReceipt)

    def test_quilt_relay_receipt_carries_patch_identities(self) -> None:
        """The one thing a quilt receipt adds over a blob receipt: an
        addressable identity for each packed blob."""
        receipt = _quilt_relay_receipt()
        assert [patch.identifier for patch in receipt.patches] == ["a.bin"]
        assert [patch.patch_id for patch in receipt.patches] == ["patch-id"]
        assert receipt.patches[0].tags == {"kind": "test"}

    def test_quilt_relay_receipt_requires_patches(self) -> None:
        """``patches`` is required rather than defaulted: assembly is
        PRE-SPEND, so a receipt carrying no layout is not a state that can
        occur, and a default would invent one."""
        with pytest.raises(TypeError):
            QuiltRelayReceipt(  # type: ignore[call-arg]
                outcome=RelayOutcome.CERTIFIED,
                blob_id="blob-id",
                object_id="object-id",
                certified=True,
                end_epoch=42,
                failed_stage=None,
                timings=_relay_timings(),
                relay_url="https://relay.example",
                tip_paid=True,
                tip_amount=100,
                register_tx_digest="digest-1",
                certify_tx_digest="digest-2",
                nonce=None,
                relay_status=None,
                relay_message=None,
                attempts=1,
                transport_error=None,
            )
