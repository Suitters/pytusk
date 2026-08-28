#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Sliver and metadata fan-out stage of the native upload pipeline.

See :mod:`pytusk.core.native_upload` (the package's ``__init__.py``) for the
full native upload pipeline description and stage ordering.
"""

import asyncio
import contextlib
import dataclasses
import logging
import random
import time

from pytusk.commands.node_commands import PutMetadata, PutSliver
from pytusk.core.certification import min_weight_for_quorum
from pytusk.core.chain import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.encoding import EncodedBlob, blob_id_to_url_base64
from pytusk.core.native_upload.common import (
    _HEARTBEAT_INTERVAL_SECONDS,
    _HEARTBEAT_SLOWEST_NODES,
)
from pytusk.core.types import SliverUploadError
from pytusk.core.types.protocols import ExecuteOnlyClient

_logger = logging.getLogger(__name__)


# --- One-shot first-failure WARNING logging (atomic via dict.setdefault) ----
# A live fan-out can produce thousands of near-identical failures (one per
# rejected sliver across ~100 nodes); logging every one at full detail
# would flood the log. A one-shot logging registry lets the FIRST failed
# sliver PUT of a given upload_slivers() run log at WARNING with full,
# untruncated detail, while every subsequent failure logs as before (INFO,
# via _upload_node's existing per-node summary).
#
# When EVERY node fails at the metadata PUT stage (see _put_metadata) before
# any sliver PUT is attempted, a separate one-shot flag guarantees the first
# metadata failure of a run is also logged at WARNING with full detail (since
# _put_sliver's one-shot never fires when no slivers are PUT at all).
#
# Test-and-set is performed via dict.setdefault on module-level _logged_once,
# which is atomic (a single GIL-protected C-level operation with no
# bytecode-level suspension point); this prevents duplicate WARNING lines
# when concurrent asyncio tasks hit a hard failure at nearly the same instant.
# ---------------------------------------------------------------------------
_logged_once: dict[str, object] = {}
"""Backing store for :func:`_log_once` -- a one-shot logging flag registry
shared across every :func:`upload_slivers` call in this process. Guards
against duplicate WARNING-level "first failure" log lines when concurrent
uploads hit a hard PUT failure at nearly the same instant.
"""


def _log_once(*, key: str) -> bool:
    """Atomically test-and-set a one-shot logging flag.

    Returns True the first time a given ``key`` is passed, and False on
    every call after -- including from concurrent asyncio tasks or, in
    principle, real OS threads. ``dict.setdefault`` on a ``str``-keyed dict
    is a single, GIL-protected C-level operation in CPython with no
    bytecode-level suspension point, so this is genuinely atomic; a plain
    ``if key not in d: d[key] = True`` pair is two separate operations and
    can race between the check and the set.

    Args:
        key (str): Identifies which one-shot flag to test-and-set (e.g.
            ``"put_failure"``, ``"metadata_failure"``).

    Returns:
        bool: True if this is the first call for ``key``, False otherwise.
    """
    sentinel = object()
    return _logged_once.setdefault(key, sentinel) is sentinel


@dataclasses.dataclass(kw_only=True, frozen=True)
class NodeUploadOutcome:
    """The result of uploading one committee member's slivers.

    Attributes:
        node_id (str): On-chain object ID of the node's staking pool.
        position (int): The node's committee position (the
            ``signers_bitmap`` index -- see
            :class:`~pytusk.core.chain.committee.WalrusCommittee`).
        weight (int): This node's shard count, reported once regardless of
            how many shards it holds.
        succeeded (bool): True only if every sliver this node holds --
            primary and secondary, for every shard it is assigned -- was
            stored successfully.
        reason (str | None): Failure description when ``succeeded`` is
            False; ``None`` on success.
    """

    node_id: str
    position: int
    weight: int
    succeeded: bool
    reason: str | None


@dataclasses.dataclass(kw_only=True, frozen=True)
class FanoutReport:
    """The outcome of a sliver fan-out across a committee.

    Attributes:
        outcomes (tuple[NodeUploadOutcome, ...]): Per-node results.
        weight_succeeded (int): Total shard weight of nodes that succeeded.
        n_shards (int): Total shard count for the committee the fan-out ran
            against.
    """

    outcomes: tuple[NodeUploadOutcome, ...]
    weight_succeeded: int
    n_shards: int

    @property
    def succeeded_positions(self) -> tuple[int, ...]:
        """Committee positions of nodes that succeeded.

        Returns:
            tuple[int, ...]: Positions of successful nodes, in the order
            their outcomes were recorded.
        """
        return tuple(outcome.position for outcome in self.outcomes if outcome.succeeded)


class _BytesInFlightThrottle:
    """Bounds total sliver PUT payload bytes concurrently in flight.

    Mirrors upstream walrus-sdk's ``max_data_in_flight`` throttle: capacity
    is BYTE-sized, not request-count-sized, because sliver size scales
    linearly with blob size and a request-count bound (the old
    ``max_in_flight`` semaphore this replaces) says nothing about how many
    megabytes are actually in flight at once.

    This is now a SECONDARY gate: :func:`upload_slivers` also computes a
    single global write-concurrency permit count (folding in
    ``max_data_in_flight`` itself, via ``max_bytes_in_flight // sliver_size``
    -- see its ``max_concurrent_writes`` paragraph) and enforces it with an
    ``asyncio.Semaphore`` acquired OUTSIDE this throttle in
    :func:`_put_sliver`. This class is kept in place regardless, both
    because it still bounds fine-grained byte accounting the coarser
    request-count-based global permit does not capture, and because
    existing unit tests reference it directly.

    EDGE CASE, load-bearing: a single sliver larger than ``max_bytes`` must
    not deadlock the throttle forever. This is handled by only blocking a
    new acquire when ``in_flight`` is already non-zero -- an acquire
    against an otherwise-idle throttle is always admitted immediately, even
    if its own size alone exceeds ``max_bytes``. It is then let through
    ALONE: every subsequent acquire blocks until it releases, since any
    addition on top of it would exceed capacity.
    """

    def __init__(self, *, max_bytes: int) -> None:
        """Initialise with the total byte capacity to enforce.

        Args:
            max_bytes (int): Maximum total payload bytes admitted
                concurrently.
        """
        self._max_bytes = max_bytes
        self._in_flight = 0
        self._condition = asyncio.Condition()

    @property
    def in_flight(self) -> int:
        """Current reserved byte count.

        Read by the heartbeat monitor (see module-level comment near
        ``_HEARTBEAT_INTERVAL_SECONDS``).

        Returns:
            int: Bytes currently reserved via :meth:`acquire` and not yet
            released via :meth:`release`.
        """
        return self._in_flight

    async def acquire(self, *, size: int) -> None:
        """Reserve ``size`` bytes of capacity, waiting if necessary.

        Args:
            size (int): Number of bytes to reserve.
        """
        async with self._condition:
            while self._in_flight > 0 and self._in_flight + size > self._max_bytes:
                await self._condition.wait()
            self._in_flight += size

    async def release(self, *, size: int) -> None:
        """Release ``size`` bytes of previously reserved capacity.

        Args:
            size (int): Number of bytes to release; must match a prior
                :meth:`acquire` call.
        """
        async with self._condition:
            self._in_flight -= size
            self._condition.notify_all()


class _FanoutProgress:
    """Live, mutable snapshot of sliver fan-out state for the heartbeat monitor.

    Feeds the heartbeat monitor (see module-level comment near
    ``_HEARTBEAT_INTERVAL_SECONDS``). Driven entirely from the single event
    loop that runs :func:`upload_slivers` -- every mutator below is called
    from a coroutine awaited on that same loop, never from a separate
    thread, so no locking is needed.
    """

    def __init__(self, *, total_nodes: int, required_weight: int) -> None:
        """Initialise an empty progress snapshot.

        Args:
            total_nodes (int): Total number of committee nodes
                participating in this fan-out.
            required_weight (int): Shard weight required to reach quorum.
        """
        self._total_nodes = total_nodes
        self._required_weight = required_weight
        self._started_at = time.monotonic()
        self._node_started_at: dict[str, float] = {}
        self._nodes_done = 0
        self._weight_succeeded = 0
        self._puts_in_flight = 0
        self._puts_completed = 0
        self._puts_failed = 0
        self._metadata_ok = 0
        self._metadata_failed = 0

    def node_started(self, *, node_id: str) -> None:
        """Record that a node's upload task has begun.

        Args:
            node_id (str): On-chain object ID of the node's staking pool.
        """
        self._node_started_at[node_id] = time.monotonic()

    def node_finished(self, *, node_id: str, succeeded: bool, weight: int) -> None:
        """Record that a node's upload task has finished.

        Args:
            node_id (str): On-chain object ID of the node's staking pool.
            succeeded (bool): Whether every sliver the node holds stored
                successfully.
            weight (int): The node's shard count, added to the succeeded
                weight total when ``succeeded`` is True.
        """
        self._node_started_at.pop(node_id, None)
        self._nodes_done += 1
        if succeeded:
            self._weight_succeeded += weight

    def put_started(self) -> None:
        """Record that one sliver PUT attempt has been dispatched."""
        self._puts_in_flight += 1

    def put_finished(self, *, succeeded: bool) -> None:
        """Record that one sliver PUT attempt has finished.

        Decrements ``_puts_in_flight`` by exactly one, pairing with the
        ``put_started()`` call for this same attempt, and increments either
        ``_puts_completed`` or ``_puts_failed`` depending on ``succeeded``.
        The caller must call this exactly once per ``put_started()`` call --
        including when the attempt raised or was cancelled -- so the two
        counters never drift apart.

        Args:
            succeeded (bool): Whether this PUT attempt ultimately succeeded.
                ``False`` covers both an HTTP failure result and an
                exception/cancellation raised while dispatching the request.
        """
        self._puts_in_flight -= 1
        if succeeded:
            self._puts_completed += 1
        else:
            self._puts_failed += 1

    def metadata_finished(self, *, succeeded: bool) -> None:
        """Record that one node's metadata PUT attempt has finished.

        Called once per node, after retries are exhausted or the metadata
        PUT succeeds -- unlike :meth:`put_started`/:meth:`put_finished`,
        there is no separate "started" counterpart, since a per-node
        metadata PUT is not chatty enough on its own to warrant in-flight
        tracking; only the final success/failure counts are surfaced (see
        :meth:`render`).

        Args:
            succeeded (bool): Whether the node's metadata PUT ultimately
                succeeded.
        """
        if succeeded:
            self._metadata_ok += 1
        else:
            self._metadata_failed += 1

    def render(self, *, bytes_in_flight: int) -> str:
        """Render a single-line summary suitable for a log record.

        Args:
            bytes_in_flight (int): Current reserved byte count from the
                shared :class:`_BytesInFlightThrottle`.

        Returns:
            str: A summary line, e.g. ``"fan-out t=12.3s nodes=17/101
            weight=280/667 meta(ok=15 fail=1) puts(inflight=8 ok=214 fail=3)
            bytes_inflight=1048576 slowest=[node_abc:11.9s,
            node_def:9.4s]"``. ``slowest`` lists up to
            ``_HEARTBEAT_SLOWEST_NODES`` still-pending nodes by elapsed
            time descending, node ids truncated to their first 12
            characters; ``slowest=[]`` when nothing is pending.
        """
        now = time.monotonic()
        elapsed = now - self._started_at
        slowest = sorted(
            self._node_started_at.items(),
            key=lambda item: now - item[1],
            reverse=True,
        )[:_HEARTBEAT_SLOWEST_NODES]
        slowest_text = ", ".join(
            f"{node_id[:12]}:{now - started_at:.1f}s"
            for node_id, started_at in slowest
        )
        return (
            f"fan-out t={elapsed:.1f}s nodes={self._nodes_done}/{self._total_nodes} "
            f"weight={self._weight_succeeded}/{self._required_weight} "
            f"meta(ok={self._metadata_ok} fail={self._metadata_failed}) "
            f"puts(inflight={self._puts_in_flight} ok={self._puts_completed} "
            f"fail={self._puts_failed}) bytes_inflight={bytes_in_flight} "
            f"slowest=[{slowest_text}]"
        )


async def _heartbeat(
    *, progress: _FanoutProgress, throttle: _BytesInFlightThrottle, interval: float
) -> None:
    """Periodically log a fan-out progress line until cancelled.

    Progress-logging coroutine (see module-level comment near
    ``_HEARTBEAT_INTERVAL_SECONDS``). Loops forever; the caller is
    responsible for cancelling it once the fan-out it is monitoring
    finishes -- it never exits on its own.

    Args:
        progress (_FanoutProgress): Progress snapshot to render each tick.
        throttle (_BytesInFlightThrottle): Shared byte throttle whose
            current in-flight reservation is included in each line.
        interval (float): Seconds to sleep between log lines.
    """
    while True:
        await asyncio.sleep(interval)
        _logger.info("%s", progress.render(bytes_in_flight=throttle.in_flight))


def _next_retry_backoff(
    *, attempt: int, retry_min_backoff: float, retry_max_backoff: float
) -> float:
    """Compute the jittered exponential backoff delay before the next retry.

    Shared by :func:`_put_sliver` and :func:`_put_metadata` so both apply
    IDENTICAL retry-backoff maths -- see :func:`upload_slivers`'s docstring
    for the policy this implements (upstream's ``ExponentialBackoffConfig``):
    backoff doubles from ``retry_min_backoff`` on each attempt, capped at
    ``retry_max_backoff``, with a small random jitter added on top so that
    concurrent retries across roughly a hundred nodes do not all wake up in
    lockstep.

    Args:
        attempt (int): Zero-based index of the attempt that just failed.
        retry_min_backoff (float): Initial backoff, in seconds, before the
            first retry.
        retry_max_backoff (float): Upper bound, in seconds, backoff growth
            is capped at.

    Returns:
        float: Seconds to sleep before the next attempt.
    """
    backoff = min(retry_min_backoff * (2**attempt), retry_max_backoff)
    return backoff + random.uniform(0, backoff * 0.25)


async def _put_metadata(
    *,
    client: ExecuteOnlyClient,
    member: WalrusCommitteeMember,
    blob_id: bytes,
    metadata_bcs: bytes,
    node_semaphore: asyncio.Semaphore,
    retry_min_backoff: float,
    retry_max_backoff: float,
    max_retries: int,
    global_write_semaphore: asyncio.Semaphore,
) -> tuple[bool, str | None]:
    """PUT a blob's metadata at a storage node, retrying transient failures.

    Mirrors :func:`_put_sliver`'s retry policy exactly (same jittered
    exponential backoff, via the shared :func:`_next_retry_backoff` helper)
    and the same semaphore acquisition order -- ``global_write_semaphore``
    first, then ``node_semaphore`` -- so a metadata PUT counts against the
    same global and per-node concurrency budgets as sliver PUTs.

    Unlike :func:`_put_sliver`, this does NOT acquire ``bytes_throttle``: a
    metadata payload is tiny relative to a sliver (per-shard digests plus a
    blob ID, see :func:`~pytusk.core.encoding.metadata_length`, not a
    symbol-sized sliver payload) and is sent exactly once per node rather
    than once per shard-pair, so folding it into the same byte budget would
    complicate the accounting for a contribution too small to matter to the
    throttle's purpose (bounding sliver payload memory).

    Args:
        client (WalrusClient): Client used to dispatch the PUT.
        member (WalrusCommitteeMember): Target storage node.
        blob_id (bytes): Raw 32-byte blob ID.
        metadata_bcs (bytes): BCS-encoded ``BlobMetadata`` payload to PUT.
        node_semaphore (asyncio.Semaphore): Bounds concurrent in-flight
            requests to this node (``max_node_connections``).
        retry_min_backoff (float): Initial backoff, in seconds, before the
            first retry.
        retry_max_backoff (float): Upper bound, in seconds, backoff growth
            is capped at.
        max_retries (int): Maximum number of retries after the initial
            attempt.
        global_write_semaphore (asyncio.Semaphore): Shared global
            write-concurrency permit (see :func:`upload_slivers`'s
            ``max_concurrent_writes`` paragraph), acquired BEFORE
            ``node_semaphore`` on every attempt.

    Returns:
        tuple[bool, str | None]: ``(succeeded, failure_reason)``. On
        failure, ``failure_reason`` is the last attempt's error message.
    """
    attempt = 0
    while True:
        async with global_write_semaphore, node_semaphore:
            # No timeout= passed: inherits the client's configured default
            # (see the module-level comment near _HEARTBEAT_INTERVAL_SECONDS).
            result = await client.execute(
                command=PutMetadata(blob_id=blob_id, metadata_bcs=metadata_bcs),
                base_url=member.base_url,
            )
        if result.is_ok():
            return True, None
        if attempt >= max_retries:
            # Log the first hard metadata-PUT failure of this run at
            # WARNING with full, untruncated detail -- see
            # the module-level comment near _log_once.
            # When every node fails at this stage, the sliver PUT one-shot
            # never fires (no sliver PUT is ever attempted),
            # so this is the only WARNING-level signal such a run produces.
            if _log_once(key="metadata_failure"):
                _logger.warning(
                    "first metadata PUT failure this run: node_id=%s "
                    "base_url=%s blob_id=%s attempts=%d reason=%s",
                    member.node_id,
                    member.base_url,
                    blob_id_to_url_base64(blob_id=blob_id),
                    attempt + 1,
                    result.result_string,
                )
            return False, result.result_string
        await asyncio.sleep(
            _next_retry_backoff(
                attempt=attempt,
                retry_min_backoff=retry_min_backoff,
                retry_max_backoff=retry_max_backoff,
            )
        )
        attempt += 1


async def _put_sliver(
    *,
    client: ExecuteOnlyClient,
    member: WalrusCommitteeMember,
    blob_id: bytes,
    sliver_pair_index: int,
    sliver_type: str,
    data: bytes,
    node_semaphore: asyncio.Semaphore,
    bytes_throttle: _BytesInFlightThrottle,
    retry_min_backoff: float,
    retry_max_backoff: float,
    max_retries: int,
    progress: _FanoutProgress,
    global_write_semaphore: asyncio.Semaphore,
) -> tuple[bool, str | None]:
    """PUT a single sliver at a storage node, retrying transient failures.

    Retries follow upstream's ``ExponentialBackoffConfig``: backoff doubles
    from ``retry_min_backoff`` on each attempt, capped at
    ``retry_max_backoff``, with a small random jitter added on top so that
    concurrent retries across roughly a hundred nodes do not all wake up in
    lockstep and hammer the network at the same instant (upstream seeds
    jitter per-node; this simpler per-call jitter serves the same purpose).
    A sliver that still fails once ``max_retries`` retries are exhausted is
    reported as a hard failure -- the caller (:func:`_upload_node`) is
    responsible for abandoning the rest of that node's uploads, matching
    upstream's ``store_pairs``.

    ``global_write_semaphore`` bounds concurrent sliver PUTs across the
    ENTIRE fan-out (upstream's ``max_concurrent_writes``/
    ``max_data_in_flight``-derived permit count -- see
    :func:`upload_slivers`'s docstring); ``node_semaphore`` bounds
    concurrent requests to THIS node specifically (``max_node_connections``);
    ``bytes_throttle`` bounds total payload bytes in flight across the
    entire fan-out. All three are acquired around every attempt, including
    retries, so a node stuck retrying does not also monopolise the global
    write permits or byte budget between attempts. Acquisition order
    matches upstream's nesting: the global write permit is acquired FIRST,
    then the per-node permit, then the attempt itself runs.

    KNOWN TRADE-OFF: because ``bytes_throttle.acquire()`` is awaited WHILE
    already holding both semaphores, a task blocked waiting for byte budget
    keeps its global and per-node permits reserved the whole time it waits
    -- narrowing effective write concurrency below what the semaphore
    counts alone would suggest, whenever the byte budget rather than the
    permit counts is the binding constraint. This is a performance
    characteristic, not a deadlock risk (the byte throttle's own EDGE CASE
    handling -- see :class:`_BytesInFlightThrottle` -- guarantees forward
    progress even when the whole budget is consumed by one oversized
    sliver). Acquiring ``bytes_throttle`` before the semaphores instead
    would only move the same coupling to a different resource (byte budget
    reserved for a PUT not yet permitted to run), not remove it, so the
    ordering is left matching upstream rather than swapped for a marginal,
    unproven gain.

    Args:
        client (WalrusClient): Client used to dispatch the PUT.
        member (WalrusCommitteeMember): Target storage node.
        blob_id (bytes): Raw 32-byte blob ID.
        sliver_pair_index (int): The URL path index -- the sliver's own
            ``sliver_pair_index``, NOT the shard index it was retrieved by.
        sliver_type (str): ``"primary"`` or ``"secondary"``.
        data (bytes): Raw BCS-encoded sliver bytes to PUT.
        node_semaphore (asyncio.Semaphore): Bounds concurrent in-flight
            PUTs to this node (``max_node_connections``).
        bytes_throttle (_BytesInFlightThrottle): Bounds total payload bytes
            in flight across the whole fan-out.
        retry_min_backoff (float): Initial backoff, in seconds, before the
            first retry.
        retry_max_backoff (float): Upper bound, in seconds, backoff growth
            is capped at.
        max_retries (int): Maximum number of retries after the initial
            attempt.
        progress (_FanoutProgress): progress tracker feeding the heartbeat
            monitor (see module-level comment near
            ``_HEARTBEAT_INTERVAL_SECONDS``)
            updated around every attempt.
        global_write_semaphore (asyncio.Semaphore): Shared global
            write-concurrency permit (see :func:`upload_slivers`'s
            ``max_concurrent_writes`` paragraph), acquired BEFORE
            ``node_semaphore`` on every attempt.

    Returns:
        tuple[bool, str | None]: ``(succeeded, failure_reason)``. On
        failure, ``failure_reason`` is the last attempt's error message.
    """
    attempt = 0
    while True:
        async with global_write_semaphore, node_semaphore:
            await bytes_throttle.acquire(size=len(data))
            progress.put_started()
            put_ok = False
            try:
                # No timeout= passed: inherits the client's configured
                # default (300s flat read timeout, upstream parity -- see
                # module-level comment near _HEARTBEAT_INTERVAL_SECONDS).
                result = await client.execute(
                    command=PutSliver(
                        blob_id=blob_id,
                        sliver_pair_index=sliver_pair_index,
                        sliver_type=sliver_type,
                        data=data,
                    ),
                    base_url=member.base_url,
                )
                put_ok = result.is_ok()
            finally:
                # put_finished() runs exactly once per put_started() above,
                # including when client.execute raised or was cancelled (in
                # which case put_ok stays False), so the in-flight counter
                # can never drift from the started/finished pairing.
                progress.put_finished(succeeded=put_ok)
                await bytes_throttle.release(size=len(data))
        if result.is_ok():
            return True, None
        if attempt >= max_retries:
            # Log the first hard sliver-PUT failure of this run at
            # WARNING with full, untruncated detail -- see the
            # module-level comment near _log_once. Every
            # later failure is left to the existing per-node INFO log in
            # _upload_node, unchanged.
            if _log_once(key="put_failure"):
                _logger.warning(
                    "first sliver PUT failure this run: node_id=%s "
                    "base_url=%s blob_id=%s sliver_pair_index=%d "
                    "sliver_type=%s attempts=%d reason=%s",
                    member.node_id,
                    member.base_url,
                    blob_id_to_url_base64(blob_id=blob_id),
                    sliver_pair_index,
                    sliver_type,
                    attempt + 1,
                    result.result_string,
                )
            return False, result.result_string
        await asyncio.sleep(
            _next_retry_backoff(
                attempt=attempt,
                retry_min_backoff=retry_min_backoff,
                retry_max_backoff=retry_max_backoff,
            )
        )
        attempt += 1


async def _upload_node(
    *,
    client: ExecuteOnlyClient,
    committee: WalrusCommittee,
    encoded: EncodedBlob,
    member: WalrusCommitteeMember,
    bytes_throttle: _BytesInFlightThrottle,
    max_node_connections: int,
    retry_min_backoff: float,
    retry_max_backoff: float,
    max_retries: int,
    progress: _FanoutProgress,
    global_write_semaphore: asyncio.Semaphore,
) -> NodeUploadOutcome:
    """Upload a node's blob metadata, then every sliver pair it is assigned.

    The node's ``PutMetadata`` is issued FIRST and awaited to completion
    (with the same retry policy as sliver PUTs -- see :func:`_put_metadata`)
    before any sliver PUT for this node is even created. If it fails after
    retries are exhausted, the WHOLE NODE is abandoned immediately with a
    reason prefixed ``"metadata: "``: NO sliver PUT is attempted for that
    node. This mirrors upstream's
    ``store_metadata_and_pairs_without_confirmation``, where a metadata
    store failure short-circuits before any sliver store is attempted --
    storage nodes reject sliver PUTs for a blob whose metadata they have
    not yet received.

    A node holding multiple shards owns multiple sliver pairs; ALL of them
    (primary and secondary, for every shard the node holds) must succeed
    before the node itself is judged successful. This is what lets
    :func:`upload_slivers` accumulate weight once per node instead of once
    per shard.

    Every sliver PUT for this node is issued concurrently, bounded to
    ``max_node_connections`` at a time by a semaphore local to this node
    (so one heavily-sharded node cannot itself absorb the entire global
    ``bytes_throttle`` budget). If any single PUT exhausts its retries and
    still fails, the WHOLE NODE is abandoned immediately: its other
    still-pending PUTs are cancelled AND awaited to completion (so their
    ``bytes_throttle`` reservations are released before this function
    returns), matching the spirit of upstream's ``store_pairs``, which
    returns on first hard failure with "could not store sliver after
    retrying; stopping storing on the node". A node's weight is never
    partially credited, so continuing doomed work for an already-failed
    node buys nothing. A sliver PUT task that RAISES instead of returning
    its ``(succeeded, reason)`` tuple is treated the same as a failed PUT
    (reason carries the exception's type and message) rather than being
    allowed to propagate and abort this whole function.

    The pending-task cancel-and-await cleanup above runs unconditionally
    in a ``finally``, so it also covers the case where THIS function's own
    task is cancelled from outside -- e.g. by :func:`upload_slivers`'s
    grace-window straggler cleanup once quorum weight is reached. Without
    this, a cancelled node task would leave its own in-flight sliver PUT
    tasks orphaned, still holding ``bytes_throttle`` reservations and
    liable to raise once the client they were dispatched through is later
    closed.

    Args:
        client (WalrusClient): Client used to dispatch PUTs.
        committee (WalrusCommittee): Committee the member belongs to, used
            to resolve its bitmap position.
        encoded (EncodedBlob): The encoded blob whose slivers are uploaded.
        member (WalrusCommitteeMember): The node to upload to.
        bytes_throttle (_BytesInFlightThrottle): Shared global byte budget.
        max_node_connections (int): Maximum concurrent PUTs to this node.
        retry_min_backoff (float): Initial per-sliver retry backoff.
        retry_max_backoff (float): Per-sliver retry backoff cap.
        max_retries (int): Maximum per-sliver retries.
        progress (_FanoutProgress): progress tracker feeding the heartbeat
            monitor (see module-level comment near
            ``_HEARTBEAT_INTERVAL_SECONDS``).
        global_write_semaphore (asyncio.Semaphore): Shared global
            write-concurrency permit, acquired by each sliver PUT BEFORE
            its per-node semaphore (see :func:`upload_slivers`'s
            ``max_concurrent_writes`` paragraph and :func:`_put_sliver`'s
            acquisition order).

    Returns:
        NodeUploadOutcome: Whether the node's metadata and every sliver it
        holds were stored.
    """
    node_start = time.monotonic()
    progress.node_started(node_id=member.node_id)
    node_semaphore = asyncio.Semaphore(max_node_connections)

    metadata_ok, metadata_reason = await _put_metadata(
        client=client,
        member=member,
        blob_id=encoded.blob_id,
        metadata_bcs=encoded.metadata_bcs,
        node_semaphore=node_semaphore,
        retry_min_backoff=retry_min_backoff,
        retry_max_backoff=retry_max_backoff,
        max_retries=max_retries,
        global_write_semaphore=global_write_semaphore,
    )
    progress.metadata_finished(succeeded=metadata_ok)
    if not metadata_ok:
        outcome = NodeUploadOutcome(
            node_id=member.node_id,
            position=committee.position_of(node_id=member.node_id),
            weight=len(member.shard_indices),
            succeeded=False,
            reason=f"metadata: {metadata_reason}",
        )
        progress.node_finished(
            node_id=member.node_id, succeeded=False, weight=outcome.weight
        )
        _logger.info("node %s failed: reason=%s", member.node_id, outcome.reason)
        return outcome

    put_specs: list[tuple[int, str, bytes]] = []
    for shard_index in member.shard_indices:
        pair = encoded.slivers[shard_index]
        put_specs.append((pair.sliver_pair_index, "primary", pair.primary))
        put_specs.append((pair.sliver_pair_index, "secondary", pair.secondary))

    tasks = [
        asyncio.create_task(
            _put_sliver(
                client=client,
                member=member,
                blob_id=encoded.blob_id,
                sliver_pair_index=sliver_pair_index,
                sliver_type=sliver_type,
                data=data,
                node_semaphore=node_semaphore,
                bytes_throttle=bytes_throttle,
                retry_min_backoff=retry_min_backoff,
                retry_max_backoff=retry_max_backoff,
                max_retries=max_retries,
                progress=progress,
                global_write_semaphore=global_write_semaphore,
            )
        )
        for sliver_pair_index, sliver_type, data in put_specs
    ]

    reason: str | None = None
    pending: set[asyncio.Task[tuple[bool, str | None]]] = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            failed_reason: str | None = None
            for task in done:
                try:
                    put_ok, put_reason = task.result()
                except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - sliver task exception must not abort fan-out
                    # A sliver PUT task raised (or, defensively, was itself
                    # cancelled by something other than this function)
                    # instead of returning its (succeeded, reason) tuple --
                    # e.g. an unexpected exception that escaped
                    # _put_sliver's own transport error handling. Treat it
                    # as a failed PUT rather than letting it re-raise here
                    # and abort the whole node (see the FIX 2 invariant in
                    # this module's robustness pass: no single sliver task
                    # may abort the fan-out). Progress accounting is
                    # unaffected on the "raised" path -- _put_sliver's own
                    # finally already called progress.put_finished() before
                    # the exception propagated out of it.
                    failed_reason = f"{type(exc).__name__}: {exc}"
                    break
                if not put_ok:
                    failed_reason = put_reason
                    break
            if failed_reason is not None:
                reason = failed_reason
                break
    finally:
        # Cancel and await to completion any sliver PUT tasks still
        # pending, whether we broke out above on a hard per-sliver
        # failure, completed normally with none pending, or THIS node's
        # own task was itself cancelled by upload_slivers's grace-window
        # straggler cleanup (see FIX 3 in this module's robustness pass).
        # A task left orphaned here keeps running detached from this
        # function, still holding its bytes_throttle reservation, and --
        # once upload_slivers or a caller further up tears down the
        # single-use WalrusClient -- eventually raises "Cannot send a
        # request, as the client has been closed." _put_sliver's own
        # try/finally already releases its bytes_throttle reservation on
        # cancellation (see _BytesInFlightThrottle and _put_sliver's
        # docstring), so no separate release is needed here.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    outcome = NodeUploadOutcome(
        node_id=member.node_id,
        position=committee.position_of(node_id=member.node_id),
        weight=len(member.shard_indices),
        succeeded=reason is None,
        reason=reason,
    )
    progress.node_finished(
        node_id=member.node_id, succeeded=outcome.succeeded, weight=outcome.weight
    )
    if outcome.succeeded:
        _logger.info(
            "node %s succeeded: weight=%d elapsed=%.1fs",
            member.node_id,
            outcome.weight,
            time.monotonic() - node_start,
        )
    else:
        _logger.info(
            "node %s failed: reason=%s", member.node_id, outcome.reason
        )
    return outcome


def _outcome_from_task(
    *,
    task: asyncio.Task[NodeUploadOutcome],
    node_id: str,
    member: WalrusCommitteeMember,
    committee: WalrusCommittee,
    progress: _FanoutProgress,
) -> NodeUploadOutcome:
    """Resolve a finished node-upload task into a :class:`NodeUploadOutcome`.

    :func:`_upload_node` returns a :class:`NodeUploadOutcome` on every path
    it controls, including its own internal failures, and calls
    :meth:`_FanoutProgress.node_finished` itself before returning (see its
    docstring). This function exists for the exceptional case a caller in
    :func:`upload_slivers` cannot rule out: the task RAISED or was
    CANCELLED instead of returning -- an unexpected exception that escaped
    every one of ``_upload_node``'s own guards, or external cancellation of
    the task itself. That case is converted into a synthesized failed
    outcome (``weight=len(member.shard_indices)``, ``reason`` carrying the
    exception's type and message) rather than letting ``task.result()``
    re-raise it into the caller and abort the whole fan-out -- see the FIX
    2 invariant in this module's robustness pass: no single node task may
    abort the fan-out. Because ``_upload_node`` never reached its own
    ``progress.node_finished()`` call on this path, this function calls it
    here instead, so every node is counted exactly once regardless of
    which path it finished on.

    Args:
        task (asyncio.Task[NodeUploadOutcome]): The finished node-upload
            task to resolve.
        node_id (str): On-chain object ID of the node's staking pool.
        member (WalrusCommitteeMember): The committee member the task was
            uploading to.
        committee (WalrusCommittee): Committee the member belongs to, used
            to resolve its bitmap position.
        progress (_FanoutProgress): progress tracker feeding the heartbeat
            monitor (see module-level comment near
            ``_HEARTBEAT_INTERVAL_SECONDS``),
            updated with a synthesized failure when this function has to
            build one itself.

    Returns:
        NodeUploadOutcome: The task's own outcome on the normal path, or a
        synthesized failed outcome if the task raised or was cancelled.
    """
    try:
        return task.result()
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - node task exception must not abort fan-out
        outcome = NodeUploadOutcome(
            node_id=node_id,
            position=committee.position_of(node_id=node_id),
            weight=len(member.shard_indices),
            succeeded=False,
            reason=f"{type(exc).__name__}: {exc}",
        )
        progress.node_finished(
            node_id=node_id, succeeded=False, weight=outcome.weight
        )
        _logger.info(
            "node %s failed (task raised %s): reason=%s",
            node_id,
            type(exc).__name__,
            outcome.reason,
        )
        return outcome


async def upload_slivers(
    *,
    client: ExecuteOnlyClient,
    committee: WalrusCommittee,
    encoded: EncodedBlob,
    grace_base_seconds: float = 0.5,
    grace_factor: float = 0.5,
    retry_min_backoff: float = 1.0,
    retry_max_backoff: float = 30.0,
    max_retries: int = 5,
    max_bytes_in_flight: int = 512_000_000,
    max_node_connections: int = 10,
    max_concurrent_writes: int = 1000,
) -> FanoutReport:
    """Fan out each node's metadata, then every sliver pair, and await quorum.

    Per node, ``PutMetadata`` is sent BEFORE any of that node's sliver PUTs
    (see :func:`_upload_node`) -- storage nodes reject sliver PUTs for a
    blob whose metadata they have not yet received, so a node whose
    metadata PUT fails after retries is abandoned entirely (weight not
    credited, reason prefixed ``"metadata: "``) with no sliver PUT
    attempted for it.

    For each shard index ``i`` in ``range(committee.n_shards)``, the pair is
    ``encoded.slivers[i]`` -- the Rust encoder already applied the
    blob-ID rotation, so entry ``i`` belongs to shard ``i``; no rotation is
    recomputed here. The target node is
    ``committee.member_for_shard(shard_index=i)``, while the URL path index
    used in the PUT is ``pair.sliver_pair_index`` -- these two are NOT the
    same value and must not be conflated. Two ``PutSliver`` commands are
    issued per pair (``"primary"``/``"secondary"``).

    Work is grouped BY NODE, not by raw shard index, before any upload
    starts: a node holding several shards is one upload task, and its
    weight (``len(member.shard_indices)``) is added to the succeeded total
    exactly once, only if every sliver across every shard it holds stored
    successfully. Iterating per shard and adding weight per shard would
    double count a multi-shard node.

    ROBUSTNESS INVARIANT: no single node task can abort this fan-out.
    ``_upload_node`` returns a :class:`NodeUploadOutcome` on every path it
    controls, but on the rare path where its task raises or is cancelled
    instead, :func:`_outcome_from_task` converts that into a synthesized
    failed outcome (reason carrying the exception's type and message)
    rather than letting it propagate here and abort every other node's
    still-running upload. Only the post-fan-out quorum check below (
    ``weight_succeeded < required_weight``) decides overall success or
    failure. On ANY exit from this function's inner ``try`` -- normal
    completion, quorum failure, or an exception propagating out (including
    this function's own task being cancelled from outside) -- the
    ``finally`` block cancels and awaits to completion every node task
    that is not yet done, so no task is ever left running detached from
    this function once it returns or raises.

    Throttling is now BYTE-based, not request-count-based:
    ``max_bytes_in_flight`` (default ``512_000_000``, matching upstream's
    ``Balanced`` upload-mode preset for ``max_data_in_flight``) bounds total
    sliver payload bytes concurrently in flight across the whole fan-out --
    see :class:`_BytesInFlightThrottle` for the oversized-single-sliver edge
    case. ``max_node_connections`` additionally caps how many of those
    bytes any ONE node may be responsible for at a time, so a single
    heavily-sharded node cannot absorb the entire global budget and starve
    every other node's PUTs.

    A SEPARATE global concurrency cap sits in front of both of the above,
    mirroring upstream's own two-gate design: a single
    ``asyncio.Semaphore`` sized to
    ``max(1, min(max_concurrent_writes, max_bytes_in_flight // sliver_size))``
    (upstream's ``limited_by`` computation) is acquired by every sliver PUT
    BEFORE its per-node semaphore and the byte throttle -- see
    :func:`_put_sliver`'s acquisition order. ``sliver_size`` is measured
    from this call's own ``encoded`` (the larger of ``encoded.slivers[0]``'s
    ``primary``/``secondary`` byte lengths -- see the fan-out start log line
    for the exact figures used on a given call).

    Each individual sliver PUT is retried on failure with exponential
    backoff (see :func:`_put_sliver`), matching upstream's
    ``ExponentialBackoffConfig``: ``retry_min_backoff`` seconds initially,
    doubling each attempt up to ``retry_max_backoff``, for at most
    ``max_retries`` retries. A sliver that still fails after retries are
    exhausted abandons its whole node (see :func:`_upload_node`) rather
    than partially crediting it.

    Concurrency policy: this call waits until succeeded weight reaches
    :func:`~pytusk.core.certification.min_weight_for_quorum`, then allows
    outstanding node uploads a DYNAMIC grace window to finish before
    cancelling anything still pending and recording it as failed with
    reason ``"cancelled"``. The window is computed as::

        extra_time = grace_base_seconds + grace_factor * time_to_quorum

    mirroring upstream's ``SliverWriteExtraTime`` (``factor=0.5``,
    ``base=500ms``), where ``time_to_quorum`` is the wall-clock time (via
    :func:`time.monotonic`, never wall-clock/civil time) elapsed from the
    start of this fan-out until quorum weight was reached. A DYNAMIC window
    beats a FIXED one because it scales with what was just observed on this
    specific network: a fast quorum gets a short, cheap grace period, while
    a slow network that took a long time to even reach quorum is
    proportionally more likely to have stragglers worth waiting a bit
    longer for, rather than guessing a single constant that is too short
    for a slow network and wastefully long for a fast one. A straggler that
    finishes within the grace window still widens the confirmation margin
    for the next stage, which is worth a short wait -- but a task left
    orphaned against a single-use
    :class:`~pytusk.client.walrus_client.WalrusClient` (see its docstring)
    is worse than cancelling it, so the wait is bounded rather than
    open-ended.

    Args:
        client (WalrusClient): Client used to dispatch sliver PUTs.
        committee (WalrusCommittee): The committee to upload against.
        encoded (EncodedBlob): The already RedStuff-encoded blob.
        grace_base_seconds (float): Fixed base component of the dynamic
            grace window (upstream's ``base=500ms``).
        grace_factor (float): Multiplier applied to the observed
            time-to-quorum when computing the dynamic grace window
            (upstream's ``factor=0.5``).
        retry_min_backoff (float): Initial per-sliver retry backoff, in
            seconds.
        retry_max_backoff (float): Per-sliver retry backoff cap, in
            seconds.
        max_retries (int): Maximum retries per sliver PUT before its node
            is abandoned.
        max_bytes_in_flight (int): Maximum total sliver payload bytes
            concurrently in flight across the whole fan-out. Defaults to
            upstream's ``Balanced`` upload-mode preset value; upstream's
            raw ``max_data_in_flight`` default (``12_500_000``) is never
            actually in effect on any real upstream config load, since
            ``apply_upload_mode_preset`` overwrites it at load time.
        max_node_connections (int): Maximum concurrent PUTs to any single
            node.
        max_concurrent_writes (int): Upper bound on the global sliver-PUT
            concurrency permit count, before it is further narrowed by the
            byte-budget-derived bound (see the ``max_concurrent_writes``
            paragraph above). Upstream's ``Balanced`` preset value.

    Returns:
        FanoutReport: Per-node outcomes and the total succeeded weight.

    Raises:
        SliverUploadError: If succeeded weight never reaches quorum for
            ``committee.n_shards``.
    """
    # Reset the one-shot first-failure flags so every run of
    # upload_slivers() gets its own verbose "first failure" sample -- see
    # the module-level comment near _log_once.
    _logged_once.pop("put_failure", None)
    _logged_once.pop("metadata_failure", None)

    bytes_throttle = _BytesInFlightThrottle(max_bytes=max_bytes_in_flight)

    # sliver_size is a representative single-sliver byte size, used only to
    # derive the global write-concurrency permit count below -- NOT the
    # per-sliver size used for the actual PUT bodies (those are read
    # per-pair from encoded.slivers in _upload_node). primary and secondary
    # sliver sizes can differ (see EncodedBlob's docstring on
    # source_symbol_counts); the LARGER of the two is used for a
    # conservative (i.e. never-too-permissive) bound.
    representative_pair = encoded.slivers[0]
    sliver_size = max(
        len(representative_pair.primary), len(representative_pair.secondary)
    )
    byte_budget_permits = max_bytes_in_flight // max(sliver_size, 1)
    if max_concurrent_writes <= byte_budget_permits:
        limited_by = "max_concurrent_writes"
    else:
        limited_by = "max_data_in_flight"
    effective_write_permits = max(1, min(max_concurrent_writes, byte_budget_permits))
    global_write_semaphore = asyncio.Semaphore(effective_write_permits)

    shards_by_node: dict[str, list[int]] = {}
    for shard_index in range(committee.n_shards):
        member = committee.member_for_shard(shard_index=shard_index)
        shards_by_node.setdefault(member.node_id, []).append(shard_index)

    members_by_node_id = {member.node_id: member for member in committee.members}

    # required_weight is computed here, BEFORE task creation, so the
    # _FanoutProgress tracker below can be constructed (and passed into
    # every _upload_node task) before any task starts.
    required_weight = min_weight_for_quorum(n_shards=committee.n_shards)
    progress = _FanoutProgress(
        total_nodes=len(shards_by_node), required_weight=required_weight
    )
    _logger.info(
        "upload_slivers start: nodes=%d required_weight=%d n_shards=%d "
        "sliver_pairs=%d max_concurrent_writes=%d max_bytes_in_flight=%d "
        "sliver_size=%d effective_write_permits=%d limited_by=%s",
        len(shards_by_node),
        required_weight,
        committee.n_shards,
        committee.n_shards,
        max_concurrent_writes,
        max_bytes_in_flight,
        sliver_size,
        effective_write_permits,
        limited_by,
    )

    start_time = time.monotonic()
    task_node_ids: dict[asyncio.Task[NodeUploadOutcome], str] = {}
    for node_id in shards_by_node:
        member = members_by_node_id[node_id]
        task = asyncio.create_task(
            _upload_node(
                client=client,
                committee=committee,
                encoded=encoded,
                member=member,
                bytes_throttle=bytes_throttle,
                max_node_connections=max_node_connections,
                retry_min_backoff=retry_min_backoff,
                retry_max_backoff=retry_max_backoff,
                max_retries=max_retries,
                progress=progress,
                global_write_semaphore=global_write_semaphore,
            )
        )
        task_node_ids[task] = node_id

    outcomes: list[NodeUploadOutcome] = []
    weight_succeeded = 0
    pending: set[asyncio.Task[NodeUploadOutcome]] = set(task_node_ids)

    # Progress heartbeat monitor -- see module-level comment near
    # _HEARTBEAT_INTERVAL_SECONDS. Must not outlive this function; the
    # finally block below cancels it unconditionally.
    heartbeat_task = asyncio.create_task(
        _heartbeat(
            progress=progress,
            throttle=bytes_throttle,
            interval=_HEARTBEAT_INTERVAL_SECONDS,
        )
    )
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                node_id = task_node_ids[task]
                outcome = _outcome_from_task(
                    task=task,
                    node_id=node_id,
                    member=members_by_node_id[node_id],
                    committee=committee,
                    progress=progress,
                )
                outcomes.append(outcome)
                if outcome.succeeded:
                    weight_succeeded += outcome.weight
            if weight_succeeded >= required_weight:
                break

        if weight_succeeded >= required_weight:
            _logger.info(
                "upload_slivers quorum reached: weight=%d/%d elapsed=%.1fs",
                weight_succeeded,
                required_weight,
                time.monotonic() - start_time,
            )

        if pending:
            time_to_quorum = time.monotonic() - start_time
            extra_time = grace_base_seconds + grace_factor * time_to_quorum
            _logger.info(
                "upload_slivers grace window: extra_time=%.1fs pending_nodes=%d",
                extra_time,
                len(pending),
            )
            done, still_pending = await asyncio.wait(pending, timeout=extra_time)
            for task in done:
                node_id = task_node_ids[task]
                outcome = _outcome_from_task(
                    task=task,
                    node_id=node_id,
                    member=members_by_node_id[node_id],
                    committee=committee,
                    progress=progress,
                )
                outcomes.append(outcome)
                if outcome.succeeded:
                    weight_succeeded += outcome.weight
            for task in still_pending:
                node_id = task_node_ids[task]
                member = members_by_node_id[node_id]
                task.cancel()
                _logger.info(
                    "upload_slivers cancelled straggler: node_id=%s", node_id
                )
                outcomes.append(
                    NodeUploadOutcome(
                        node_id=node_id,
                        position=committee.position_of(node_id=node_id),
                        weight=len(member.shard_indices),
                        succeeded=False,
                        reason="cancelled",
                    )
                )
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

        # Cancel and await to completion any node-upload task not yet
        # done, on ANY exit from the try block above -- normal completion
        # (where this is a no-op, since every task was already resolved or
        # explicitly cancelled+awaited via the still_pending handling),
        # quorum failure, or an exception (including this function's OWN
        # task being cancelled from outside) propagating before that
        # handling was reached. Checked directly against task_node_ids
        # (every task this call created) rather than the loop-local
        # `pending`/`still_pending` names, since those may not even be
        # bound yet if the exception happened early. A leftover task here
        # would otherwise run on, detached, still holding its share of
        # bytes_throttle and (via its own _upload_node's node_semaphore
        # and pending sliver tasks) the node-level reservations described
        # in _upload_node's docstring, and would raise "Cannot send a
        # request, as the client has been closed." once a caller further
        # up tears down the single-use WalrusClient.
        leftover = [task for task in task_node_ids if not task.done()]
        if leftover:
            for task in leftover:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*leftover, return_exceptions=True)

    if weight_succeeded < required_weight:
        failures_by_reason: dict[str, int] = {}
        for outcome in outcomes:
            if not outcome.succeeded:
                key = outcome.reason or "unknown"
                failures_by_reason[key] = failures_by_reason.get(key, 0) + 1
        failure_summary = ", ".join(
            f"{reason}={count}" for reason, count in sorted(failures_by_reason.items())
        )
        raise SliverUploadError(
            message=(
                f"Sliver fan-out reached weight {weight_succeeded}, requires "
                f"{required_weight} for n_shards={committee.n_shards}; failures "
                f"by reason: {failure_summary or 'none'}"
            ),
            stage="upload_slivers",
        )

    return FanoutReport(
        outcomes=tuple(outcomes),
        weight_succeeded=weight_succeeded,
        n_shards=committee.n_shards,
    )
