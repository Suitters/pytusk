#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the blob-status result types."""

import dataclasses

import pytest

from pytusk.core.types.blob_status import (
    BlobStatusReport,
    DeletableCounts,
    DeletableStatus,
    DissentReason,
    EventRef,
    InvalidStatus,
    NodeDissent,
    NodeRef,
    NonexistentStatus,
    PermanentStatus,
    Resolution,
    UnresolvedStatus,
)

_EVENT = EventRef(tx_digest="DaRmjvMBZoU6cU1iZUM3qMdakFYa5jJcmWFyaxW2aReN", event_seq=0)
_COUNTS = DeletableCounts(total=0, certified=0)


def _permanent(*, end_epoch: int = 508) -> PermanentStatus:
    """Build a PermanentStatus with the given expiry."""
    return PermanentStatus(
        end_epoch=end_epoch,
        is_certified=True,
        status_event=_EVENT,
        deletable_counts=_COUNTS,
        initial_certified_epoch=507,
    )


class TestVariantsAreFrozen:
    """Every status variant is immutable, so a report cannot be mutated."""

    @pytest.mark.parametrize(
        "status",
        [
            NonexistentStatus(),
            UnresolvedStatus(),
            InvalidStatus(status_event=_EVENT),
            DeletableStatus(deletable_counts=_COUNTS, initial_certified_epoch=None),
            _permanent(),
        ],
        ids=lambda s: type(s).__name__,
    )
    def test_cannot_assign(self, status: object) -> None:
        """Assigning to any field raises."""
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.__setattr__("end_epoch", 1)


class TestStatusesGroupByEquality:
    """Identical answers from different nodes must collapse to one key.

    This is what lets the fan-out accumulate shard weight per distinct
    status with a plain dict and no synthetic key -- so it is a load-bearing
    property, not an incidental one.
    """

    def test_equal_statuses_hash_equal(self) -> None:
        """Two separately built but identical verdicts are one dict key."""
        tally = {_permanent(): 10}
        tally[_permanent()] = tally.get(_permanent(), 0) + 7
        assert tally == {_permanent(): 17}

    def test_differing_statuses_are_distinct_keys(self) -> None:
        """A different end_epoch is a different answer, not the same one."""
        tally = {_permanent(end_epoch=508): 1, _permanent(end_epoch=509): 1}
        assert len(tally) == 2

    def test_unit_variants_are_equal_to_their_own_kind(self) -> None:
        """Field-less variants still compare and hash by type."""
        assert NonexistentStatus() == NonexistentStatus()
        assert len({NonexistentStatus(), NonexistentStatus()}) == 1
        assert NonexistentStatus() != UnresolvedStatus()


class TestEnumWireValues:
    """Enum values reach the CLI verbatim, so they are part of the contract."""

    def test_resolution_values(self) -> None:
        """Resolution renders lowercase, matching the CLI summary line."""
        assert Resolution.QUORUM.value == "quorum"
        assert Resolution.VALIDITY.value == "validity"
        assert Resolution.UNRESOLVED.value == "unresolved"

    def test_dissent_reason_values(self) -> None:
        """The five-value taxonomy uses hyphens, not underscores."""
        assert DissentReason.NOT_STORED.value == "not-stored"
        assert DissentReason.DISAGREED.value == "disagreed"
        assert DissentReason.TIMEOUT.value == "timeout"
        assert DissentReason.ERROR.value == "error"
        assert DissentReason.NOT_CHECKED.value == "not-checked"

    def test_str_is_the_bare_value(self) -> None:
        """str() must not render as 'ClassName.MEMBER'."""
        assert str(Resolution.QUORUM) == "quorum"
        assert str(DissentReason.NOT_CHECKED) == "not-checked"


class TestReportShape:
    """BlobStatusReport carries the verdict AND the evidence behind it."""

    def test_carries_both_node_lists(self) -> None:
        """confirming and dissenting are separate tuples, both present."""
        node = NodeRef(node_id="0x01", network_address="n:9185", weight=7)
        report = BlobStatusReport(
            blob_id=b"\x01" * 32,
            status=_permanent(),
            resolution=Resolution.QUORUM,
            committee_epoch=507,
            confirming=(node,),
            dissenting=(
                NodeDissent(node=node, reason=DissentReason.ERROR),
            ),
        )
        assert report.confirming[0].weight == 7
        assert report.dissenting[0].reason is DissentReason.ERROR

    def test_unresolved_pairs_with_unresolved_status(self) -> None:
        """No-verdict is carried by the shape rather than raised."""
        report = BlobStatusReport(
            blob_id=b"\x02" * 32,
            status=UnresolvedStatus(),
            resolution=Resolution.UNRESOLVED,
            committee_epoch=507,
            confirming=(),
            dissenting=(),
        )
        assert isinstance(report.status, UnresolvedStatus)
        assert report.resolution is Resolution.UNRESOLVED
