#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Committee-wide blob-status query.

One storage node's answer is an opinion, never a verdict: a node can be
stale, byzantine, or simply unaware of a recent registration. This module
fans :class:`~pytusk.commands.node_commands.ReadBlobStatus` across the whole
committee and resolves the answers against SHARD-WEIGHT thresholds.

Two thresholds, in order:

1. **Quorum** (2f+1 weight on one status) -- the strong answer.
2. **Validity** (f+1 weight on one status) -- weak, but enough that at
   least one honest node reported it. Free: a second check over answers
   already in hand, no extra network and no chain call. Omitting it would
   report "could not tell" while holding evidence good enough to answer.

If neither clears, the query returns
:class:`~pytusk.core.types.blob_status.UnresolvedStatus` rather than
raising. No-verdict is a normal outcome of a distributed query, and the
evidence matters MOST in that case -- raising would either discard it or
force stuffing it onto an exception.

On-chain event verification (which the reference Rust client attempts as a
third tier) is deliberately NOT performed: it cannot apply to deletable
blobs at all, and it verifies a CHAIN fact rather than a storage one,
adding no confidence about whether slivers are actually retrievable.
"""

import asyncio
import logging
import time

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import GetObject, GetObjectsOwnedByAddress, SuiRpcResult

from pytusk.commands.node_commands import ReadBlobStatus
from pytusk.core.certification import min_weight_for_quorum, min_weight_for_validity
from pytusk.core.chain.blob_fields import blob_id_from_object
from pytusk.core.chain.committee import WalrusCommitteeMember, fetch_committee
from pytusk.core.chain.events import fetch_event_object_id
from pytusk.core.committee_fanout import fan_out_to_committee
from pytusk.core.types.blob_status import (
    BlobStatus,
    BlobStatusReport,
    DissentReason,
    NodeDissent,
    NodeRef,
    NonexistentStatus,
    PermanentStatus,
    Resolution,
    UnresolvedStatus,
)
from pytusk.core.types.protocols import CommitteeAndNodeClient

_logger = logging.getLogger(__name__)

# Per-REQUEST timeout, deliberately a module constant rather than a
# parameter. It is a property of "a healthy node answers a status read
# fast", not a knob a caller has any basis to tune. The whole-operation
# deadline IS a parameter -- see fetch_blob_status's timeout_seconds.
_PER_REQUEST_TIMEOUT_SECONDS = 3.0

def _certainty_rank(*, status: BlobStatus) -> int:
    """Rank a status by how strong a claim it is, lower being stronger.

    Used only to break ties when several statuses each clear the validity
    threshold. Mirrors the reference client, which sorts most-certain
    (``Invalid``) to least (``Nonexistent``).

    Args:
        status (BlobStatus): The status to rank.

    Returns:
        int: Rank, 0 being the strongest claim.
    """
    name = type(status).__name__
    order = {
        "InvalidStatus": 0,
        "PermanentStatus": 1,
        "DeletableStatus": 2,
        "NonexistentStatus": 3,
    }
    return order.get(name, 4)


def _node_ref(*, member: WalrusCommitteeMember) -> NodeRef:
    """Project a committee member down to what a status report needs.

    Args:
        member (WalrusCommitteeMember): The committee member.

    Returns:
        NodeRef: Identity plus shard weight, without the BLS public key.
    """
    return NodeRef(
        node_id=member.node_id,
        network_address=member.network_address,
        weight=len(member.shard_indices),
    )


async def _status_from_node(
    *,
    client: CommitteeAndNodeClient,
    member: WalrusCommitteeMember,
    blob_id: bytes,
    semaphore: asyncio.Semaphore,
) -> tuple[BlobStatus | None, DissentReason | None]:
    """Ask one node for its blob-status opinion.

    NEVER raises for a node-level problem: a single unreachable or
    misbehaving node must not abort the fan-out. Every failure comes back
    as a ``(None, reason)`` pair. ``asyncio.CancelledError`` is
    deliberately NOT caught -- cancellation is the driver's mechanism for
    closing the grace window and must propagate.

    Args:
        client (CommitteeAndNodeClient): Transport.
        member (WalrusCommitteeMember): The node to ask.
        blob_id (bytes): Raw 32-byte blob ID.
        semaphore (asyncio.Semaphore): Bounds concurrent in-flight reads.

    Returns:
        tuple[BlobStatus | None, DissentReason | None]: The node's status,
            or None with the reason it did not supply one.
    """
    async with semaphore:
        try:
            result: SuiRpcResult = await client.execute(
                command=ReadBlobStatus(blob_id=blob_id),
                base_url=member.base_url,
                timeout=_PER_REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one node must not abort the fan-out
            _logger.debug(
                "blob_status node error: node_id=%s error=%s", member.node_id, exc
            )
            return None, DissentReason.ERROR

    if not result.is_ok():
        _logger.debug(
            "blob_status node unusable: node_id=%s reason=%s",
            member.node_id,
            result.result_string,
        )
        return None, DissentReason.ERROR
    status = result.result_data
    if status is None:
        return None, DissentReason.ERROR
    return status, None


async def fetch_blob_status(
    *,
    client: CommitteeAndNodeClient,
    blob_id: bytes,
    staking_object: str,
    timeout_seconds: float = 10.0,
    max_concurrent_requests: int = 64,
) -> BlobStatusReport:
    """Resolve a blob's status across the storage committee.

    Args:
        client (CommitteeAndNodeClient): Transport, used both to fetch the
            committee from chain and to query each node by URL.
        blob_id (bytes): Raw 32-byte blob ID.
        staking_object (str): Walrus staking object ID, for the committee
            fetch.
        timeout_seconds (float): WHOLE-OPERATION deadline. Not a
            per-request timeout -- that is an internal constant. Because
            the fan-out stops early once a threshold is met, this mostly
            governs stragglers and the path to
            :class:`~pytusk.core.types.blob_status.UnresolvedStatus`, not
            the common case.
        max_concurrent_requests (int): Ceiling on in-flight node reads.

    Returns:
        BlobStatusReport: The verdict, which threshold produced it, and the
            full accounting of every committee member. Returned for every
            outcome including no-verdict; this function does not raise to
            report an unresolved status.
    """
    started = time.monotonic()
    committee = await fetch_committee(reader=client, staking_object=staking_object)

    required_quorum = min_weight_for_quorum(n_shards=committee.n_shards)
    required_validity = min_weight_for_validity(n_shards=committee.n_shards)

    # Statuses are frozen dataclasses, so equal answers hash equal and
    # group themselves -- no synthetic key needed.
    weight_by_status: dict[BlobStatus, int] = {}
    nodes_by_status: dict[BlobStatus, list[NodeRef]] = {}
    failures: list[NodeDissent] = []
    answered: set[str] = set()

    semaphore = asyncio.Semaphore(max_concurrent_requests)
    tasks: dict[
        asyncio.Task[tuple[BlobStatus | None, DissentReason | None]],
        WalrusCommitteeMember,
    ] = {}
    for member in committee.members:
        task = asyncio.create_task(
            _status_from_node(
                client=client, member=member, blob_id=blob_id, semaphore=semaphore
            )
        )
        tasks[task] = member

    def _fold(
        task: asyncio.Task[tuple[BlobStatus | None, DissentReason | None]],
        member: WalrusCommitteeMember,
    ) -> None:
        """Fold one finished node result into the accumulators."""
        answered.add(member.node_id)
        node = _node_ref(member=member)
        if task.cancelled():
            failures.append(NodeDissent(node=node, reason=DissentReason.NOT_CHECKED))
            return
        error = task.exception()
        if error is not None:
            _logger.debug(
                "blob_status node raised: node_id=%s error=%s", member.node_id, error
            )
            failures.append(NodeDissent(node=node, reason=DissentReason.ERROR))
            return
        status, reason = task.result()
        if status is None:
            failures.append(
                NodeDissent(node=node, reason=reason or DissentReason.ERROR)
            )
            return
        weight_by_status[status] = weight_by_status.get(status, 0) + node.weight
        nodes_by_status.setdefault(status, []).append(node)

    def _cancelled(
        task: asyncio.Task[tuple[BlobStatus | None, DissentReason | None]],
        member: WalrusCommitteeMember,
    ) -> None:
        """Record a straggler cancelled when the grace window closed."""
        answered.add(member.node_id)
        failures.append(
            NodeDissent(node=_node_ref(member=member), reason=DissentReason.TIMEOUT)
        )

    def _quorum_reached() -> bool:
        """True once any single status holds quorum weight."""
        return any(weight >= required_quorum for weight in weight_by_status.values())

    def _log_threshold(elapsed: float) -> None:
        _logger.info(
            "blob_status quorum reached: required=%d elapsed=%.1fs",
            required_quorum,
            elapsed,
        )

    _logger.info(
        "blob_status start: nodes=%d n_shards=%d required_quorum=%d "
        "required_validity=%d timeout=%.1fs",
        len(committee.members),
        committee.n_shards,
        required_quorum,
        required_validity,
        timeout_seconds,
    )

    remaining = timeout_seconds - (time.monotonic() - started)
    try:
        await asyncio.wait_for(
            fan_out_to_committee(
                tasks=tasks,
                on_completed=_fold,
                on_cancelled=_cancelled,
                should_stop=_quorum_reached,
                label="blob_status",
                on_threshold_reached=_log_threshold,
            ),
            timeout=max(remaining, 0.0),
        )
    except asyncio.TimeoutError:
        # The deadline is an ordinary outcome, not an error. Whatever was
        # folded before it fired still stands; everything else is
        # reconciled as unaccounted-for below.
        _logger.info(
            "blob_status deadline reached: timeout=%.1fs answered=%d/%d",
            timeout_seconds,
            len(answered),
            len(committee.members),
        )

    # Every member must appear in exactly one of the two output lists, so
    # the report always accounts for the whole committee. A member missing
    # here was never folded -- the deadline fired first.
    for member in committee.members:
        if member.node_id not in answered:
            failures.append(
                NodeDissent(
                    node=_node_ref(member=member), reason=DissentReason.NOT_CHECKED
                )
            )

    verdict, resolution = _resolve(
        weight_by_status=weight_by_status,
        required_quorum=required_quorum,
        required_validity=required_validity,
    )

    confirming = tuple(nodes_by_status.get(verdict, ()))
    dissenting = list(failures)
    for status, nodes in nodes_by_status.items():
        if status == verdict:
            continue
        # A node reporting Nonexistent has not disagreed about details --
        # it has said it holds nothing, which is its own reason.
        reason = (
            DissentReason.NOT_STORED
            if isinstance(status, NonexistentStatus)
            else DissentReason.DISAGREED
        )
        dissenting.extend(NodeDissent(node=node, reason=reason) for node in nodes)

    _logger.info(
        "blob_status done: resolution=%s confirming=%d dissenting=%d elapsed=%.1fs",
        resolution,
        len(confirming),
        len(dissenting),
        time.monotonic() - started,
    )

    return BlobStatusReport(
        blob_id=blob_id,
        status=verdict,
        resolution=resolution,
        committee_epoch=committee.epoch,
        confirming=confirming,
        dissenting=tuple(dissenting),
    )


def _resolve(
    *,
    weight_by_status: dict[BlobStatus, int],
    required_quorum: int,
    required_validity: int,
) -> tuple[BlobStatus, Resolution]:
    """Pick the verdict from accumulated per-status weight.

    Quorum first. Failing that, validity, breaking ties by certainty so a
    strong claim outranks a weak one at equal standing. Failing both,
    no verdict.

    Args:
        weight_by_status (dict[BlobStatus, int]): Shard weight per distinct
            status reported.
        required_quorum (int): 2f+1 threshold in shard weight.
        required_validity (int): f+1 threshold in shard weight.

    Returns:
        tuple[BlobStatus, Resolution]: The verdict and how it was reached.
            ``UnresolvedStatus`` pairs only with ``Resolution.UNRESOLVED``.
    """
    for status, weight in weight_by_status.items():
        if weight >= required_quorum:
            return status, Resolution.QUORUM

    candidates = [
        status
        for status, weight in weight_by_status.items()
        if weight >= required_validity
    ]
    if candidates:
        candidates.sort(key=lambda status: _certainty_rank(status=status))
        return candidates[0], Resolution.VALIDITY

    return UnresolvedStatus(), Resolution.UNRESOLVED


async def resolve_blob_sui_objects(
    *,
    client: CommitteeAndNodeClient,
    blob_id: bytes,
    owner: str,
    report: BlobStatusReport,
    known_blob_sui_object: sui_prot.Object | None = None,
) -> tuple[list[sui_prot.Object], int]:
    """Resolve a blob_id to its on-chain blob_sui_object(s) via the tiered ladder.

    Layers on-chain detail atop a committee status verdict, in four tiers of
    decreasing certainty:

    1. Nothing resolved (returned list stays empty).
    2. ``known_blob_sui_object`` was already in hand (e.g. from an explicit
       ``-o``/object-id lookup), or is found among ``owner``'s owned objects.
    3. Unowned but the blob is a ``PermanentStatus`` registration -- resolved
       via its certification event. Never available for a deletable blob,
       which carries no status event.
    4. Nothing could be resolved at all.

    This is a lossless extraction of the ladder `tusky blob_status` has
    always run -- behavior here matches that command's prior inline logic
    exactly, so both it and any other caller (e.g. a future gate that must
    short-circuit on an expired blob) see identical resolution.

    Args:
        client (CommitteeAndNodeClient): Transport for the owned-objects scan
            and any object fetch this needs.
        blob_id (bytes): Raw 32-byte blob ID being resolved.
        owner (str): Active Sui address to scan for owned Blob objects.
        report (BlobStatusReport): The already-fetched committee verdict;
            drives tier-3 eligibility (``PermanentStatus`` only) and supplies
            the status event it resolves through.
        known_blob_sui_object (sui_prot.Object | None): An already-resolved
            Blob object (e.g. from an explicit object-id lookup), folded in
            as an immediate tier-2 result with no extra network calls.

    Returns:
        tuple[list[sui_prot.Object], int]: Resolved blob_sui_object(s) (may
            be empty) and the tier at which resolution stopped (1-4).
    """
    blob_sui_objects: list[sui_prot.Object] = []
    if known_blob_sui_object is not None:
        blob_sui_objects.append(known_blob_sui_object)

    tier = 2 if blob_sui_objects else 1
    if not blob_sui_objects:
        # Tier 2: one scan returns every sibling blob_sui_object, so owning
        # any object for this blob yields ALL of them with no follow-up.
        owned = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
        if owned.is_ok():
            for obj in owned.result_data.objects:
                if not (obj.object_type and "::blob::Blob" in obj.object_type):
                    continue
                try:
                    if blob_id_from_object(obj=obj) == blob_id:
                        blob_sui_objects.append(obj)
                except ValueError:
                    # One malformed object must not sink the listing.
                    continue
        if blob_sui_objects:
            tier = 2

    if not blob_sui_objects and isinstance(report.status, PermanentStatus):
        # Tier 3: resolve an UNOWNED permanent blob to its object via
        # the status event. Sui objects are publicly readable, so
        # ownership gates nothing. Never available for deletable --
        # that variant carries no event.
        object_id = await fetch_event_object_id(
            reader=client,
            tx_digest=report.status.status_event.tx_digest,
            event_seq=report.status.status_event.event_seq,
        )
        if object_id is not None:
            found = await client.execute(command=GetObject(object_id=object_id))
            if found.is_ok():
                blob_sui_objects.append(found.result_data)
                tier = 3

    if not blob_sui_objects:
        tier = 4

    return blob_sui_objects, tier
