#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the committee-wide blob-status resolution.

Thresholds are computed over SHARD WEIGHT, never node count. The committee
below is deliberately uneven -- 10 nodes holding 100 shards in unequal
shares -- so a test that accidentally counted nodes would fail rather than
coincidentally pass.
"""

import asyncio

import pytest
from pysui import SuiRpcResult

import pytusk.core.ops.blob_status as blob_status_module
from pytusk.core.chain.committee import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.ops.blob_status import fetch_blob_status
from pytusk.core.types.blob_status import (
    DeletableCounts,
    DissentReason,
    EventRef,
    NonexistentStatus,
    PermanentStatus,
    Resolution,
    UnresolvedStatus,
)

# 10 nodes, 100 shards, uneven split -> quorum 67, validity 34.
_SHARD_SPLIT = [20, 15, 12, 11, 10, 9, 8, 6, 5, 4]


def _committee() -> WalrusCommittee:
    """Build the uneven 10-node / 100-shard committee."""
    members = []
    start = 0
    for index, width in enumerate(_SHARD_SPLIT):
        members.append(
            WalrusCommitteeMember(
                node_id=f"0x{index:02x}",
                shard_indices=tuple(range(start, start + width)),
                network_address=f"node{index}:9185",
                public_key=b"\x00" * 48,
            )
        )
        start += width
    return WalrusCommittee(epoch=507, n_shards=100, members=tuple(members))


_PERMANENT = PermanentStatus(
    end_epoch=508,
    is_certified=True,
    status_event=EventRef(tx_digest="D", event_seq=0),
    deletable_counts=DeletableCounts(total=0, certified=0),
    initial_certified_epoch=507,
)


class _FakeCommitteeClient:
    """WalrusClient-shaped fake for the blob-status fan-out.

    ``answers`` maps a node's base_url to either a status (returned OK), the
    string "error" (returned as a failed result), or a float (seconds to
    sleep before answering, for exercising the deadline).
    """

    def __init__(self, *, answers: dict) -> None:
        self.answers = answers

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        answer = self.answers.get(base_url)
        if answer is None:
            raise RuntimeError("unreachable node")
        if answer == "error":
            return SuiRpcResult(False, "node said no")
        if isinstance(answer, float):
            await asyncio.sleep(answer)
            return SuiRpcResult(True, "", _PERMANENT)
        return SuiRpcResult(True, "", answer)

    async def execute_for_all(
        self, *, command: object, timeout: float | None = None, headers: dict | None = None
    ) -> SuiRpcResult:
        raise AssertionError("committee fetch is patched out in these tests")


@pytest.fixture
def patched_committee(monkeypatch: pytest.MonkeyPatch) -> WalrusCommittee:
    """Replace the chain committee fetch with the fixed fake committee."""
    committee = _committee()

    async def _fake_fetch(*, reader: object, staking_object: str) -> WalrusCommittee:
        return committee

    monkeypatch.setattr(blob_status_module, "fetch_committee", _fake_fetch)
    return committee


def _urls(committee: WalrusCommittee, count: int) -> list[str]:
    """Base URLs of the first ``count`` members."""
    return [member.base_url for member in committee.members[:count]]


async def _run(client: _FakeCommitteeClient, **kwargs):
    """Invoke fetch_blob_status with the fake client."""
    return await fetch_blob_status(
        client=client, blob_id=b"\x01" * 32, staking_object="0xstake", **kwargs
    )


class TestThresholds:
    """Quorum, then validity, then no verdict."""

    async def test_quorum_when_weight_clears_two_thirds(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """20+15+12+11+10 = 68 >= 67, so quorum on five of ten nodes."""
        answers = {url: _PERMANENT for url in _urls(patched_committee, 5)}
        report = await _run(_FakeCommitteeClient(answers=answers))
        assert report.resolution is Resolution.QUORUM
        assert isinstance(report.status, PermanentStatus)

    async def test_validity_when_weight_clears_only_one_third(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """20+15 = 35, over validity (34) but under quorum (67).

        This is the path live testing could never reach: the window between
        f+1 and 2f+1 is too narrow to hit by tuning a timeout.
        """
        answers = {url: _PERMANENT for url in _urls(patched_committee, 2)}
        report = await _run(_FakeCommitteeClient(answers=answers))
        assert report.resolution is Resolution.VALIDITY
        assert isinstance(report.status, PermanentStatus)

    async def test_unresolved_below_validity(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """A single 20-shard node is under 34, so no verdict is returned."""
        answers = {url: _PERMANENT for url in _urls(patched_committee, 1)}
        report = await _run(_FakeCommitteeClient(answers=answers))
        assert report.resolution is Resolution.UNRESOLVED
        assert isinstance(report.status, UnresolvedStatus)

    async def test_no_verdict_is_returned_not_raised(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """The evidence matters most here, so it comes back on the report."""
        report = await _run(_FakeCommitteeClient(answers={}))
        assert report.resolution is Resolution.UNRESOLVED
        assert report.committee_epoch == 507
        assert len(report.dissenting) == 10


class TestDissentClassification:
    """A node that never answered has NOT disagreed."""

    async def test_nonexistent_answer_is_not_stored(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """Answering 'I hold nothing' is its own reason, not 'disagreed'."""
        answers = {url: _PERMANENT for url in _urls(patched_committee, 5)}
        for url in _urls(patched_committee, 7)[5:]:
            answers[url] = NonexistentStatus()
        report = await _run(_FakeCommitteeClient(answers=answers))
        reasons = {d.reason for d in report.dissenting}
        assert DissentReason.NOT_STORED in reasons
        assert DissentReason.DISAGREED not in reasons

    async def test_unusable_response_is_error(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """A failed result is an error, never silent agreement."""
        answers = {url: _PERMANENT for url in _urls(patched_committee, 5)}
        answers[patched_committee.members[5].base_url] = "error"
        report = await _run(_FakeCommitteeClient(answers=answers))
        assert DissentReason.ERROR in {d.reason for d in report.dissenting}


class TestCommitteeAccounting:
    """Every member appears exactly once, on exactly one list."""

    @pytest.mark.parametrize("answering", [0, 1, 2, 5, 10])
    async def test_all_members_accounted_for(
        self, patched_committee: WalrusCommittee, answering: int
    ) -> None:
        """confirming + dissenting always covers the whole committee."""
        answers = {url: _PERMANENT for url in _urls(patched_committee, answering)}
        report = await _run(_FakeCommitteeClient(answers=answers))
        seen = [node.node_id for node in report.confirming] + [
            d.node.node_id for d in report.dissenting
        ]
        assert sorted(seen) == sorted(m.node_id for m in patched_committee.members)

    async def test_weight_comes_from_shard_count(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """NodeRef.weight is shards held, not 1-per-node."""
        answers = {url: _PERMANENT for url in _urls(patched_committee, 5)}
        report = await _run(_FakeCommitteeClient(answers=answers))
        assert sorted(n.weight for n in report.confirming) == [10, 11, 12, 15, 20]


class TestDeadline:
    """timeout_seconds bounds the WHOLE operation, not each request."""

    async def test_slow_nodes_do_not_block_past_the_deadline(
        self, patched_committee: WalrusCommittee
    ) -> None:
        """Nodes still in flight at the deadline are reported, not awaited."""
        answers = {url: 30.0 for url in _urls(patched_committee, 10)}
        report = await asyncio.wait_for(
            _run(_FakeCommitteeClient(answers=answers), timeout_seconds=0.2),
            timeout=10.0,
        )
        assert report.resolution is Resolution.UNRESOLVED
        assert len(report.dissenting) == 10
