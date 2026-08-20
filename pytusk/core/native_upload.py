#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Native Walrus upload orchestration: sliver fan-out, confirmation
collection, certification, and the thin end-to-end compose.

The native upload order is FIXED and must not be rearranged::

    encode -> reserve_space+register_blob (Tx1) -> upload metadata+slivers ->
    collect confirmations -> certify_blob (Tx2)

Storage nodes REJECT sliver PUTs for an unregistered blob, so Tx1 must
complete before any sliver is uploaded. Storage nodes ALSO reject sliver
PUTs for a blob whose metadata they have not yet received -- see
:func:`_upload_node`, which PUTs a node's ``PutMetadata`` before any of
that node's sliver PUTs and abandons the node entirely (no sliver PUT
attempted) if the metadata PUT fails. This module owns everything AFTER
encoding (:mod:`pytusk.core.encoding`) and Tx1
(:mod:`pytusk.core.system_ops`): the per-node metadata+sliver fan-out, the
confirmation quorum collection, the Tx2 submission, and a thin end-to-end
compose function that runs all of it in order.

Weight is always a SHARD COUNT, never a node count -- see
:func:`~pytusk.core.certification.is_quorum` and
:func:`~pytusk.core.certification.min_weight_for_quorum`. A committee
member holding several shards contributes its weight exactly once; grouping
work by node rather than by raw shard index is what keeps that true.

FOUR DIFFERENT ENCODINGS OF 32-BYTE IDENTIFIERS ARE IN PLAY ACROSS NATIVE
UPLOAD, and mixing them up produces failures that look like signature
corruption or a malformed request, not a type error:

- blob ID, 32 bytes -> URL-safe UNPADDED base64 (storage-node URL paths;
  see :func:`~pytusk.core.encoding.blob_id_to_url_base64`)
- blob ID, 32 bytes -> little-endian ``u256`` (``register_blob`` Move
  argument; see :func:`~pytusk.core.encoding.blob_id_to_u256`)
- object ID, 32 bytes -> ``"0x"`` + 64 hex characters (the deletable
  confirmation URL path, passed through as-is by
  :class:`~pytusk.commands.node_commands.GetStorageConfirmation`)
- object ID, 32 bytes -> 32 RAW bytes (see :func:`object_id_to_raw_bytes`;
  used inside the signed confirmation message, never in a URL)
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import random
import time
from collections.abc import Sequence

from pytusk.client.walrus_client import WalrusClient
from pytusk.commands.node_commands import GetStorageConfirmation, PutMetadata, PutSliver
from pytusk.core.certification import (
    Certificate,
    ConfirmationMismatchError,
    InvalidConfirmationError,
    NodeConfirmation,
    QuorumNotReachedError,
    build_certificate,
    confirmation_message,
    min_weight_for_quorum,
    verify_certificate,
)
from pytusk.core.committee import (
    WalrusCommittee,
    WalrusCommitteeMember,
    fetch_committee,
    fetch_epoch,
)
from pytusk.core.encoding import EncodedBlob, blob_id_to_url_base64, encode_blob
from pytusk.core.system_ops import (
    Registration,
    execute_certify,
    execute_reserve_and_register,
)
from pytusk.core.utils import resolve_package_id

__all__ = [
    "CertifyTransactionError",
    "ConfirmationCollectionError",
    "EpochMismatchError",
    "FanoutReport",
    "NativeBlobReceipt",
    "NativeUploadError",
    "NodeUploadOutcome",
    "SliverUploadError",
    "StageTimings",
    "assert_certificate_epoch_current",
    "certify",
    "collect_confirmations",
    "object_id_to_raw_bytes",
    "store_blob_native",
    "upload_slivers",
]

_logger = logging.getLogger(__name__)


# --- Progress heartbeat logging ------------------------------------------
# A live sliver fan-out or confirmation collection can run for many
# minutes against ~100 storage nodes with no other output in between. The
# heartbeat monitor below logs a periodic one-line progress summary (node/
# weight/byte-throttle counts) at INFO so a run in progress is observable.
# Every client.execute(...) call site in this module passes no timeout=,
# inheriting WalrusClient's configured default (see
# pytusk.client.walrus_client._DEFAULT_TIMEOUT: a flat 300s read timeout,
# upstream parity), which comfortably covers GetStorageConfirmation's
# wait_millis long-polls without a per-call computed override.
# ---------------------------------------------------------------------------

_HEARTBEAT_INTERVAL_SECONDS: float = 2.0
_HEARTBEAT_SLOWEST_NODES: int = 5

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


def object_id_to_raw_bytes(*, object_id: str) -> bytes:
    """Convert a Sui object ID string into its 32 raw bytes.

    Sui object IDs are ``0x`` followed by 64 hex characters (32 bytes).
    This is a DIFFERENT identifier and a DIFFERENT encoding from the Walrus
    blob ID -- do not conflate the two. It is needed for the deletable-blob
    branch of :func:`~pytusk.core.certification.confirmation_message`'s
    ``object_id`` argument: the SIGNED MESSAGE requires the raw bytes, while
    the ``GetStorageConfirmation`` URL path
    (:class:`~pytusk.commands.node_commands.GetStorageConfirmation`) takes
    the same object ID as the ``0x...`` string, unconverted. See the module
    docstring's four-row table for the complete set of 32-byte identifier
    encodings in play across native upload and how they differ.

    Args:
        object_id (str): A Sui object ID, ``0x`` followed by 64 hex
            characters.

    Returns:
        bytes: The 32 raw bytes the object ID encodes.

    Raises:
        ValueError: If ``object_id`` is not well-formed hex, or does not
            decode to exactly 32 bytes.
    """
    text = object_id[2:] if object_id.startswith(("0x", "0X")) else object_id
    try:
        decoded = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"object_id {object_id!r} is not valid hex") from exc
    if len(decoded) != 32:
        raise ValueError(
            f"object_id {object_id!r} decoded to {len(decoded)} bytes, expected 32"
        )
    return decoded


@dataclasses.dataclass(kw_only=True, frozen=True)
class NodeUploadOutcome:
    """The result of uploading one committee member's slivers.

    Attributes:
        node_id (str): On-chain object ID of the node's staking pool.
        position (int): The node's committee position (the
            ``signers_bitmap`` index -- see
            :class:`~pytusk.core.committee.WalrusCommittee`).
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


@dataclasses.dataclass(kw_only=True, frozen=True)
class StageTimings:
    """Wall-clock duration, in seconds, of each native upload pipeline stage.

    Captured via :func:`time.monotonic` (never wall/civil time -- see
    :func:`upload_slivers`'s docstring for the same reasoning), so these
    figures are immune to clock adjustments and safe to difference.

    Every field is ``float | None``: ``None`` is a real, meaningful value
    meaning the stage never ran -- e.g. ``tusky store_blob_native --mode
    simulate`` stops after Tx1, and a failed pipeline run stops at whatever
    stage broke. A stage that did not run must never be reported as
    ``0.0``, which would be indistinguishable from "ran and took no time".

    A stage's duration is recorded even when that stage FAILS: callers that
    populate this dataclass do so from a ``try``/``finally`` around the
    stage's call, so a sliver fan-out that ran for 40 minutes before dying
    still reports that 40 minutes here.

    Attributes:
        encode (float | None): Duration of the ``encode_blob`` call.
        register_tx1 (float | None): Duration of
            :func:`~pytusk.core.system_ops.execute_reserve_and_register`
            (Tx1: ``reserve_space`` + ``register_blob``).
        sliver_upload (float | None): Duration of :func:`upload_slivers`.
        confirmations (float | None): Duration of
            :func:`collect_confirmations`.
        certify_tx2 (float | None): Duration of the ``execute_certify``
            call inside :func:`certify` (Tx2: ``certify_blob``).
        total (float | None): Total wall-clock duration of the whole
            pipeline invocation that produced these timings, from its own
            entry point to its return -- not merely the sum of the other
            fields, since it also captures unattributed overhead between
            stages (e.g. committee/package-ID reads).
    """

    encode: float | None
    register_tx1: float | None
    sliver_upload: float | None
    confirmations: float | None
    certify_tx2: float | None
    total: float | None


@dataclasses.dataclass(kw_only=True, frozen=True)
class NativeBlobReceipt:
    """The outcome of a native upload attempt.

    There is deliberately NO ``storage_object_id`` field. The ``Storage``
    returned by ``reserve_space`` is consumed BY VALUE inside Tx1 and
    wrapped into the created ``Blob`` object -- it is never transferred to
    an address and never becomes an independently addressable object (see
    :class:`~pytusk.core.system_ops.Registration`'s docstring for the
    same reasoning). There is therefore no standalone Storage object ID to
    report here either; do not re-add this field.

    Attributes:
        blob_id (str): The blob ID as URL-safe, unpadded base64 (see
            :func:`~pytusk.core.encoding.blob_id_to_url_base64`).
        object_id (str): Object ID of the ``Blob`` created by
            ``register_blob``.
        certified (bool): True once ``certify_blob`` has succeeded.
        end_epoch (int): The blob's storage expiration epoch.
        failed_stage (str | None): Name of the pipeline stage that failed,
            when this receipt represents a partial/failed attempt; ``None``
            on a fully certified upload.
        timings (StageTimings): Wall-clock duration of each pipeline stage
            that ran before this receipt was produced -- populated on both
            the fully-certified and the partial/failed path (see
            :class:`StageTimings`).
    """

    blob_id: str
    object_id: str
    certified: bool
    end_epoch: int
    failed_stage: str | None
    timings: StageTimings


class NativeUploadError(RuntimeError):
    """Base error for a failed stage of the native upload pipeline.

    Attributes:
        stage (str): Name of the pipeline stage that failed (e.g.
            ``"upload_slivers"``, ``"collect_confirmations"``,
            ``"certify"``).
        duration (float | None): Wall-clock duration, in seconds, of the
            raising site's own timed work before it failed, when the
            raising site tracked one and chose to report it; ``None``
            otherwise (e.g. a failure discovered before that stage's timed
            work began, or a stage whose duration
            :func:`store_blob_native` already tracks itself via its own
            local ``try``/``finally`` and therefore does not need repeated
            here).
    """

    stage: str
    duration: float | None

    def __init__(
        self, *, message: str, stage: str, duration: float | None = None
    ) -> None:
        """Initialise with a human-readable message, the failing stage, and
        an optional stage duration.

        Args:
            message (str): Human-readable description of the failure.
            stage (str): Name of the pipeline stage that failed.
            duration (float | None): Wall-clock duration, in seconds, of
                the raising site's own timed work before it failed, if
                tracked; ``None`` when not applicable.
        """
        super().__init__(message)
        self.stage = stage
        self.duration = duration


class SliverUploadError(NativeUploadError):
    """Raised when sliver fan-out failed to reach quorum weight."""


class ConfirmationCollectionError(NativeUploadError):
    """Raised when the confirmation-collection stage fails.

    Covers a quorum of storage-node confirmations not being gathered, and
    also wraps :class:`~pytusk.core.certification.ConfirmationMismatchError`
    and :class:`~pytusk.core.certification.InvalidConfirmationError` when
    :func:`~pytusk.core.certification.build_certificate` rejects the
    collected confirmations -- see :func:`collect_confirmations`'s
    docstring.
    """


class EpochMismatchError(NativeUploadError):
    """Raised when the on-chain epoch kept moving and certification retries
    were exhausted."""


class CertifyTransactionError(NativeUploadError):
    """Raised when Tx2 (``certify_blob``) fails to submit, aborts on-chain,
    fails pysui's pre-submission gas-estimation dry run, or when local
    certificate verification rejects the certificate before Tx2 is even
    attempted.

    Wraps TWO different bare exception types that
    :func:`~pytusk.core.system_ops.execute_certify` (and the pysui
    machinery it calls) can raise: a bare ``RuntimeError`` on a submission
    failure or an on-chain abort, and a bare ``ValueError`` when pysui's
    gas-estimation dry run inside ``txn.build_and_sign()`` fails (pysui's
    ``txn_gas.py`` raises ``ValueError(f"Error running
    SimulateTransactionKind: {result.result_string}")`` in that case, not
    ``RuntimeError``). See :func:`certify`'s docstring for the full
    ``Raises`` note. ``system_ops`` deliberately does not depend on
    ``native_upload`` (see that module's docstring), so the conversion from
    either bare exception type to this :class:`NativeUploadError` subclass
    happens here, on the ``native_upload`` side of that boundary, inside
    :func:`certify` -- not in ``execute_certify`` itself. The original error
    text is preserved verbatim as this exception's message.
    """


async def assert_certificate_epoch_current(
    *, client: WalrusClient, committee: WalrusCommittee, staking_object: str
) -> None:
    """Raise if the live on-chain epoch has moved off ``committee.epoch``.

    A :class:`~pytusk.core.certification.Certificate`'s ``signers_bitmap``
    positions are meaningful only against the committee ordering it was
    built from (see :class:`~pytusk.core.certification.Certificate`'s
    docstring). If the Walrus epoch advances between when confirmations
    were collected and when Tx2 is submitted, the committee may have
    reordered and the certificate's bitmap positions no longer identify the
    same nodes -- ``certify_blob`` would abort. This performs a single
    cheap epoch read (:func:`~pytusk.core.committee.fetch_epoch`) so a
    caller composing Tx2 by hand via
    :func:`~pytusk.core.system_ops.add_certify` can check freshness
    before submitting, rather than discovering the mismatch from an
    on-chain abort after gas is spent.

    This is a CHECK ONLY -- unlike :func:`certify` (see its docstring), it
    does not refetch the committee, re-collect confirmations, or rebuild a
    certificate on a mismatch. A caller composing Tx2 by hand is
    responsible for redoing that work themselves if this raises;
    :func:`certify`'s existing retry behaviour is unchanged and remains the
    recommended path for callers who want that handled automatically.

    Args:
        client (WalrusClient): Client used to read the live epoch.
        committee (WalrusCommittee): The committee ``certificate`` (the one
            about to be submitted via
            :func:`~pytusk.core.system_ops.add_certify`) was built
            against.
        staking_object (str): Object ID of the configured Walrus staking
            object.

    Raises:
        EpochMismatchError: If the live on-chain epoch differs from
            ``committee.epoch``.
        RuntimeError: Propagated from
            :func:`~pytusk.core.committee.fetch_epoch` if the epoch
            cannot be read.
    """
    current_epoch = await fetch_epoch(reader=client, staking_object=staking_object)
    if current_epoch != committee.epoch:
        raise EpochMismatchError(
            message=(
                f"On-chain epoch {current_epoch} differs from the committee "
                f"epoch {committee.epoch}; a certificate built against the "
                "latter's signer-bitmap ordering is no longer valid for "
                "certify_blob."
            ),
            stage="certify",
        )


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
    client: WalrusClient,
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
    client: WalrusClient,
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
    client: WalrusClient,
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
    client: WalrusClient,
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


class _ConfirmProgress:
    """Live, mutable snapshot of confirmation-collection progress for the
    heartbeat monitor.

    Feeds the heartbeat monitor (see module-level comment near
    ``_HEARTBEAT_INTERVAL_SECONDS``). Driven entirely from the single event
    loop that runs :func:`collect_confirmations`'s confirmation-collection
    tasks, so no locking is needed.
    """

    def __init__(self, *, total: int, required_weight: int) -> None:
        """Initialise an empty confirmation-progress snapshot.

        Args:
            total (int): Total number of confirmation candidates queried.
            required_weight (int): Shard weight required to reach quorum.
        """
        self.total = total
        self.completed = 0
        self.failed = 0
        self._required_weight = required_weight
        self._weight_confirmed = 0
        self._started_at = time.monotonic()

    def finished(self, *, ok: bool, weight: int) -> None:
        """Record that one confirmation request has completed.

        Args:
            ok (bool): Whether the node returned a usable confirmation.
            weight (int): The node's shard count, added to the confirmed
                weight total when ``ok`` is True.
        """
        self.completed += 1
        if not ok:
            self.failed += 1
        else:
            self._weight_confirmed += weight

    def render(self) -> str:
        """Render a single-line summary suitable for a log record.

        Returns:
            str: A summary line, e.g. ``"confirmations t=3.1s done=44/101
            weight=280/667 ok=41 fail=3"``.
        """
        elapsed = time.monotonic() - self._started_at
        ok = self.completed - self.failed
        return (
            f"confirmations t={elapsed:.1f}s done={self.completed}/{self.total} "
            f"weight={self._weight_confirmed}/{self._required_weight} "
            f"ok={ok} fail={self.failed}"
        )


async def _confirm_heartbeat(*, progress: _ConfirmProgress, interval: float) -> None:
    """Periodically log a confirmation-collection progress line until
    cancelled.

    Progress-logging coroutine (see module-level comment near
    ``_HEARTBEAT_INTERVAL_SECONDS``). Loops forever; the caller is
    responsible for cancelling it once confirmation collection finishes --
    it never exits on its own.

    Args:
        progress (_ConfirmProgress): Progress snapshot to render each tick.
        interval (float): Seconds to sleep between log lines.
    """
    while True:
        await asyncio.sleep(interval)
        _logger.info("%s", progress.render())


async def _confirm_node(
    *,
    client: WalrusClient,
    committee: WalrusCommittee,
    member: WalrusCommitteeMember,
    blob_id: bytes,
    object_id: str | None,
    wait_millis: int | None,
    semaphore: asyncio.Semaphore,
    confirm_progress: _ConfirmProgress,
) -> tuple[NodeConfirmation | None, str | None]:
    """Request one node's storage confirmation and adapt it to
    :class:`~pytusk.core.certification.NodeConfirmation`.

    Args:
        client (WalrusClient): Client used to dispatch the request.
        committee (WalrusCommittee): Committee the member belongs to, used
            to resolve its bitmap position.
        member (WalrusCommitteeMember): The node to query.
        blob_id (bytes): Raw 32-byte blob ID.
        object_id (str | None): Blob object ID for a deletable blob, else
            ``None``.
        wait_millis (int | None): Upper bound in milliseconds for the
            node's long-poll wait.
        semaphore (asyncio.Semaphore): Bounds concurrent in-flight requests.
        confirm_progress (_ConfirmProgress): progress tracker feeding the
            heartbeat monitor (see module-level comment near
            ``_HEARTBEAT_INTERVAL_SECONDS``), updated with this request's
            outcome.

    Returns:
        tuple[NodeConfirmation | None, str | None]: ``(confirmation,
        reason)``. ``confirmation`` is ``None`` if the request failed, in
        which case ``reason`` carries the diagnostic failure message;
        ``reason`` is ``None`` on success.
    """
    # No timeout= passed: inherits the client's configured default (300s
    # flat read timeout, upstream parity -- see module-level comment near
    # _HEARTBEAT_INTERVAL_SECONDS), which comfortably covers this request's
    # server-side long poll (wait_millis; GetStorageConfirmation is sent
    # with wait_for_registration=True).
    async with semaphore:
        result = await client.execute(
            command=GetStorageConfirmation(
                blob_id=blob_id,
                object_id=object_id,
                wait_for_registration=True,
                wait_millis=wait_millis,
            ),
            base_url=member.base_url,
        )
    if not result.is_ok():
        confirm_progress.finished(ok=False, weight=len(member.shard_indices))
        return None, result.result_string
    signed = result.result_data
    confirm_progress.finished(ok=True, weight=len(member.shard_indices))
    return (
        NodeConfirmation(
            node_id=member.node_id,
            position=committee.position_of(node_id=member.node_id),
            weight=len(member.shard_indices),
            public_key=member.public_key,
            serialized_message=signed.serialized_message,
            signature=signed.signature,
        ),
        None,
    )


def _confirmation_outcome_from_task(
    *,
    task: asyncio.Task[tuple[NodeConfirmation | None, str | None]],
    member: WalrusCommitteeMember,
    confirm_progress: _ConfirmProgress,
) -> tuple[NodeConfirmation | None, str | None]:
    """Resolve a finished confirmation task into its outcome.

    :func:`_confirm_node` returns its ``(confirmation, reason)`` outcome on
    every path it controls, but on the rare path where its task raises or
    is cancelled instead, this converts that into a synthesized failed
    outcome (``reason`` carrying the exception's type and message) rather
    than letting ``task.result()`` re-raise it into
    :func:`collect_confirmations` and abort the whole confirmation stage --
    mirrors :func:`_outcome_from_task`'s guard around ``upload_slivers``'s
    own per-node tasks. Because ``_confirm_node`` never reached its own
    ``confirm_progress.finished()`` call on this path, this function calls
    it here instead, so every node is counted exactly once regardless of
    which path it finished on.

    Args:
        task (asyncio.Task[tuple[NodeConfirmation | None, str | None]]):
            The finished confirmation task to resolve.
        member (WalrusCommitteeMember): The committee member the task was
            querying, used for weight/logging on the synthesized-failure
            path.
        confirm_progress (_ConfirmProgress): progress tracker feeding the
            heartbeat monitor, updated with a synthesized failure when this
            function has to build one itself.

    Returns:
        tuple[NodeConfirmation | None, str | None]: The task's own outcome
        on the normal path, or ``(None, reason)`` if the task raised or was
        cancelled.
    """
    try:
        return task.result()
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - node task exception must not abort confirmation collection
        reason = f"{type(exc).__name__}: {exc}"
        confirm_progress.finished(ok=False, weight=len(member.shard_indices))
        _logger.info(
            "confirmation for node %s failed (task raised %s): reason=%s",
            member.node_id,
            type(exc).__name__,
            reason,
        )
        return None, reason


async def collect_confirmations(
    *,
    client: WalrusClient,
    committee: WalrusCommittee,
    blob_id: bytes,
    registration: Registration,
    positions: Sequence[int] | None = None,
    wait_millis: int | None = None,
    max_confirmation_requests: int = 64,
    grace_base_seconds: float = 0.5,
    grace_factor: float = 0.5,
) -> Certificate:
    """Collect a quorum of storage-node confirmations and build a certificate.

    Queries ``GetStorageConfirmation`` against every committee member by
    DEFAULT, regardless of that node's sliver-upload outcome. This is the
    RECOMMENDED behaviour and matches upstream policy: a failed sliver
    upload does not prove a node lacks the slivers -- it may have obtained
    them some other way, or the upload error may have been spurious -- and
    a node's confirmation request failing here simply does not count
    toward the confirmation quorum, rather than being assumed impossible
    and skipped. ``positions`` exists only as an OPT-IN narrowing (e.g. to
    retry a specific subset after diagnosing a prior partial failure); it
    is not, and should not be used as, a way to pre-filter out nodes whose
    upload failed. ``wait_for_registration=True`` is always sent: this is
    SERVER-SIDE long-polling that replaces client-side backoff for the
    registration-propagation race between Tx1 committing and a storage
    node observing it.

    A per-node confirmation failure is TOLERATED, not raised: it is logged
    and simply excluded from the confirmation set, exactly like a missing
    confirmation from a node that was never queried. Only a genuine quorum
    shortfall across the whole queried set raises
    :class:`ConfirmationCollectionError` -- see ``Raises`` below.

    The expected confirmation message is built INDEPENDENTLY via
    :func:`~pytusk.core.certification.confirmation_message`, rather than
    trusting the first responding node's message as the reference -- for a
    deletable blob, ``registration.object_id`` is converted to its 32
    raw bytes via :func:`object_id_to_raw_bytes` first (the message needs
    raw bytes, not the ``0x...`` string used in the confirmation URL path).

    Concurrency policy: a request is DISPATCHED to every candidate member
    up front (matching the "queries every member" behaviour above), but
    this call does not necessarily WAIT for every one of them to finish.
    Mirroring :func:`upload_slivers`'s own quorum early-exit (see its
    "Concurrency policy" paragraph), confirmation results are processed as
    they complete and confirmed shard weight (``confirmation.weight``, the
    same ``len(member.shard_indices)`` source used everywhere else in this
    module) is accumulated running; once it reaches
    :func:`~pytusk.core.certification.min_weight_for_quorum`, this stops
    waiting for the rest. Outstanding requests are then given the SAME
    dynamic grace window formula as :func:`upload_slivers`::

        extra_time = grace_base_seconds + grace_factor * time_to_quorum

    before any still-pending request is cancelled and reported the same way
    an ordinary failed confirmation is: with a "returned no usable
    confirmation" warning (``reason="cancelled"``). This is the fix for a
    flat ~5.4s per-upload confirmation-collection cost observed against
    testnet, where roughly 31 of 101 committee nodes are unreachable and
    only fail after a connect timeout -- waiting for literally every
    candidate, as the previous single ``asyncio.gather`` did, meant every
    upload paid that cost regardless of blob size. When quorum is NOT
    reached from the members that respond before their tasks are otherwise
    done, this call keeps waiting for every remaining candidate exactly as
    before (the grace window only ever narrows what is waited for, never
    what is dispatched), so the failure-path diagnostics below are
    unaffected by this change.

    Args:
        client (WalrusClient): Client used to dispatch requests.
        committee (WalrusCommittee): The committee to query.
        blob_id (bytes): Raw 32-byte Walrus blob ID confirmations are
            sought for.
        registration (Registration): Tx1's result, identifying the blob and
            whether it is deletable.
        positions (Sequence[int] | None): OPT-IN narrowing to specific
            committee positions; ``None`` (the default and recommended
            setting) queries every member regardless of upload outcome.
        wait_millis (int | None): Upper bound in milliseconds for each
            node's long-poll wait.
        max_confirmation_requests (int): Maximum concurrent confirmation
            requests.
        grace_base_seconds (float): Fixed base component of the dynamic
            grace window applied once quorum weight is reached (upstream's
            ``base=500ms`` -- see :func:`upload_slivers`'s docstring for the
            same constant).
        grace_factor (float): Multiplier applied to the observed
            time-to-quorum when computing the dynamic grace window
            (upstream's ``factor=0.5`` -- see :func:`upload_slivers`'s
            docstring for the same constant).

    Returns:
        Certificate: The quorum-backed certificate.

    Raises:
        ConfirmationCollectionError: If no node returned a usable
            confirmation, the collected confirmations do not reach quorum,
            or :func:`~pytusk.core.certification.build_certificate` raises
            :class:`ConfirmationMismatchError` (nodes disagree on the
            confirmation message) or :class:`InvalidConfirmationError` (a
            confirmation's signature fails verification) -- both are
            wrapped into this single exception type so every
            confirmation-stage failure reaches the same
            :class:`NativeUploadError` recovery path. A minority of
            tolerated per-node failures does NOT raise this on its own, so
            long as the remaining confirmations still reach quorum.
        ValueError: Propagated from ``build_certificate`` for a malformed
            confirmation set (e.g. duplicate committee positions).
    """
    object_id_bytes = (
        object_id_to_raw_bytes(object_id=registration.object_id)
        if registration.deletable
        else None
    )
    expected_message = confirmation_message(
        epoch=committee.epoch, blob_id=blob_id, object_id=object_id_bytes
    )

    candidates = (
        committee.members
        if positions is None
        else tuple(committee.members[position] for position in positions)
    )

    # No per-call timeout is computed here anymore -- every _confirm_node
    # call inherits the client's configured default (see its own comment),
    # which comfortably covers wait_millis long-polls, so there is nothing
    # request-specific left to report in this log line.
    _logger.info(
        "collect_confirmations start: candidates=%d wait_millis=%s",
        len(candidates),
        wait_millis,
    )

    required_weight = min_weight_for_quorum(n_shards=committee.n_shards)
    confirm_progress = _ConfirmProgress(total=len(candidates), required_weight=required_weight)
    semaphore = asyncio.Semaphore(max_confirmation_requests)
    # Progress heartbeat monitor -- see module-level comment near
    # _HEARTBEAT_INTERVAL_SECONDS. Must not outlive this function; the
    # finally block below cancels it unconditionally.
    heartbeat_task = asyncio.create_task(
        _confirm_heartbeat(
            progress=confirm_progress, interval=_HEARTBEAT_INTERVAL_SECONDS
        )
    )

    # QUORUM EARLY-EXIT: a request is dispatched to every candidate up
    # front, exactly as before, but this loop stops WAITING once confirmed
    # weight reaches quorum rather than always waiting for literally every
    # candidate -- mirroring upload_slivers's own quorum early-exit (see
    # this function's "Concurrency policy" docstring paragraph and
    # upload_slivers's own collection loop, which this is deliberately kept
    # structurally identical to).
    start_time = time.monotonic()
    task_members: dict[
        asyncio.Task[tuple[NodeConfirmation | None, str | None]], WalrusCommitteeMember
    ] = {}
    for member in candidates:
        task = asyncio.create_task(
            _confirm_node(
                client=client,
                committee=committee,
                member=member,
                blob_id=blob_id,
                object_id=(
                    registration.object_id if registration.deletable else None
                ),
                wait_millis=wait_millis,
                semaphore=semaphore,
                confirm_progress=confirm_progress,
            )
        )
        task_members[task] = member

    confirmations: list[NodeConfirmation] = []
    weight_confirmed = 0
    pending: set[asyncio.Task[tuple[NodeConfirmation | None, str | None]]] = set(
        task_members
    )
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                member = task_members[task]
                confirmation, reason = _confirmation_outcome_from_task(
                    task=task, member=member, confirm_progress=confirm_progress
                )
                if confirmation is None:
                    _logger.warning(
                        "Storage node %s (%s) returned no usable confirmation "
                        "(reason=%s); tolerated so long as quorum is still met "
                        "from the rest",
                        member.node_id,
                        member.base_url,
                        reason,
                    )
                else:
                    confirmations.append(confirmation)
                    weight_confirmed += confirmation.weight
            if weight_confirmed >= required_weight:
                break

        if weight_confirmed >= required_weight:
            _logger.info(
                "collect_confirmations quorum reached: weight=%d/%d elapsed=%.1fs",
                weight_confirmed,
                required_weight,
                time.monotonic() - start_time,
            )

        # Grace window for stragglers still in flight once quorum is
        # reached -- same dynamic formula as upload_slivers (see its
        # docstring; grace_base_seconds/grace_factor default to the SAME
        # upstream values, 500ms/0.5, and are not new timing values). If
        # quorum was never reached above, `pending` is already empty here
        # (the while loop only exits early on quorum; otherwise it runs
        # until every candidate is done), so this block is a no-op on the
        # failure path and every candidate's result has already been
        # processed above exactly as it was before this change.
        if pending:
            time_to_quorum = time.monotonic() - start_time
            extra_time = grace_base_seconds + grace_factor * time_to_quorum
            _logger.info(
                "collect_confirmations grace window: extra_time=%.1fs "
                "pending_nodes=%d",
                extra_time,
                len(pending),
            )
            done, still_pending = await asyncio.wait(pending, timeout=extra_time)
            for task in done:
                member = task_members[task]
                confirmation, reason = _confirmation_outcome_from_task(
                    task=task, member=member, confirm_progress=confirm_progress
                )
                if confirmation is None:
                    _logger.warning(
                        "Storage node %s (%s) returned no usable confirmation "
                        "(reason=%s); tolerated so long as quorum is still met "
                        "from the rest",
                        member.node_id,
                        member.base_url,
                        reason,
                    )
                else:
                    confirmations.append(confirmation)
                    weight_confirmed += confirmation.weight
            for task in still_pending:
                member = task_members[task]
                task.cancel()
                _logger.info(
                    "collect_confirmations cancelled straggler: node_id=%s",
                    member.node_id,
                )
                _logger.warning(
                    "Storage node %s (%s) returned no usable confirmation "
                    "(reason=%s); tolerated so long as quorum is still met "
                    "from the rest",
                    member.node_id,
                    member.base_url,
                    "cancelled",
                )
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

        # Cancel and await to completion any confirmation task not yet
        # done, on ANY exit from the try block above -- mirrors
        # upload_slivers's own leftover-task cleanup (see its docstring's
        # ROBUSTNESS INVARIANT paragraph and its identical finally block).
        # A leftover task here would otherwise run on, detached, and
        # eventually raise once a caller further up tears down the
        # single-use WalrusClient.
        leftover = [task for task in task_members if not task.done()]
        if leftover:
            for task in leftover:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*leftover, return_exceptions=True)

    _logger.info(
        "collect_confirmations done: usable=%d/%d",
        len(confirmations),
        len(candidates),
    )

    if not confirmations:
        raise ConfirmationCollectionError(
            message=(
                f"No storage node returned a usable confirmation out of "
                f"{len(candidates)} candidates queried"
            ),
            stage="collect_confirmations",
        )

    try:
        return await asyncio.to_thread(
            functools.partial(
                build_certificate,
                confirmations=confirmations,
                committee_size=committee.committee_size,
                n_shards=committee.n_shards,
                expected_message=expected_message,
            )
        )
    except (
        QuorumNotReachedError,
        ConfirmationMismatchError,
        InvalidConfirmationError,
    ) as exc:
        raise ConfirmationCollectionError(
            message=str(exc), stage="collect_confirmations"
        ) from exc


async def certify(
    *,
    client: WalrusClient,
    committee: WalrusCommittee,
    blob_id: bytes,
    registration: Registration,
    certificate: Certificate,
    package_id: str,
    system_object: str,
    staking_object: str,
    sender: str | None = None,
    sponsor: str | None = None,
    recipient: str | None = None,
    max_attempts: int = 2,
    stage_timings: StageTimings | None = None,
) -> NativeBlobReceipt:
    """Submit Tx2 (``certify_blob``), refetching on an epoch mismatch.

    Before submitting, the current on-chain epoch is re-read via the cheap
    :func:`~pytusk.core.committee.fetch_epoch`. If it differs from
    ``committee.epoch``, that is treated as REFETCH-AND-RETRY, not a
    signature failure: the committee is refetched, confirmations are
    re-collected via :func:`collect_confirmations`, and the bitmap is
    REBUILT against the new committee ordering. The old certificate is
    never reused across an epoch change -- signer positions are ordering
    dependent, and ordering changes with the committee.

    Args:
        client (WalrusClient): Client used to submit the transaction and
            query the epoch/committee.
        committee (WalrusCommittee): The committee ``certificate`` was
            built against.
        blob_id (bytes): Raw 32-byte Walrus blob ID being certified. The
            base64 form used in the returned receipt is derived from this
            via :func:`~pytusk.core.encoding.blob_id_to_url_base64`.
        registration (Registration): Tx1's result, identifying the Blob.
        certificate (Certificate): The quorum-backed certificate to submit.
        package_id (str): Walrus package ID.
        system_object (str): Object ID of the configured Walrus System
            object.
        staking_object (str): Object ID of the configured Walrus staking
            object, used to re-read the epoch/committee on a mismatch.
        sender (str | None): Address to sign Tx2 as. Defaults to the active
            address when ``None``. Must be the current owner of the ``Blob``
            being certified -- see
            :func:`~pytusk.core.system_ops.execute_reserve_and_register`'s
            docstring for why Tx1 always transfers it there.
        sponsor (str | None): Address to sponsor Tx2's gas as, or ``None``
            for no sponsorship.
        recipient (str | None): Sui address to transfer the now-certified
            ``Blob`` to, in the same PTB, immediately after
            ``certify_blob`` -- passed straight through to
            :func:`~pytusk.core.system_ops.execute_certify`. When ``None``
            (the default), no transfer happens and the ``Blob`` stays with
            ``sender``.
        max_attempts (int): Maximum number of epoch checks before giving up.
        stage_timings (StageTimings | None): Durations already recorded for
            earlier pipeline stages (``encode``, ``register_tx1``,
            ``sliver_upload``, ``confirmations``) by the caller, carried
            through into the returned receipt's ``timings`` alongside this
            call's own ``certify_tx2`` duration. ``None`` (the default)
            starts from a :class:`StageTimings` with every field ``None``,
            for a caller (e.g. a standalone ``tusky certify_blob`` recovery
            run) that has no earlier stages to report.

    Returns:
        NativeBlobReceipt: The certified blob's receipt.

    Raises:
        EpochMismatchError: If the on-chain epoch still disagrees with the
            committee after ``max_attempts`` checks.
        CertifyTransactionError: If Tx2 (``certify_blob``) fails to submit,
            aborts on-chain, or fails pysui's pre-submission gas-estimation
            dry run. pysui signals these with TWO DIFFERENT exception
            types, and both must be caught here: a submission failure or
            on-chain abort inside :func:`~pytusk.core.system_ops.execute_certify`
            itself raises a bare ``RuntimeError``, while a gas-estimation/
            simulate failure inside ``txn.build_and_sign()`` -- which
            ``execute_certify`` calls before ever submitting -- raises
            ``ValueError`` from pysui's ``txn_gas.py``
            (``ValueError(f"Error running SimulateTransactionKind: ...")``).
            Both are converted here with the original message preserved and
            this call's own ``certify_tx2`` duration attached as
            ``duration``. ``NativeUploadError`` (this exception's own base
            class) is a ``RuntimeError`` subclass, but ``execute_certify``
            cannot itself raise one -- ``system_ops`` deliberately has no
            dependency on ``native_upload`` (see that module's docstring) --
            so this ``except`` clause cannot double-wrap an
            already-wrapped ``NativeUploadError``. Also raised, with
            ``duration=None`` (Tx2 timing has not started yet), if
            :func:`~pytusk.core.certification.verify_certificate` finds the
            certificate's aggregate signature does not verify against the
            committee's public keys for its signer positions -- this local
            check runs before Tx2 is ever attempted, so a bad certificate
            fails for free instead of spending gas on-chain.
    """
    incoming_timings = stage_timings or StageTimings(
        encode=None,
        register_tx1=None,
        sliver_upload=None,
        confirmations=None,
        certify_tx2=None,
        total=None,
    )
    current_committee = committee
    current_certificate = certificate
    attempts = 0

    while True:
        attempts += 1
        current_epoch = await fetch_epoch(reader=client, staking_object=staking_object)
        if current_epoch == current_committee.epoch:
            break
        if attempts >= max_attempts:
            raise EpochMismatchError(
                message=(
                    f"On-chain epoch {current_epoch} still differs from the "
                    f"committee epoch {current_committee.epoch} after "
                    f"{attempts} attempt(s); giving up."
                ),
                stage="certify",
            )
        current_committee = await fetch_committee(
            reader=client, staking_object=staking_object
        )
        current_certificate = await collect_confirmations(
            client=client,
            committee=current_committee,
            blob_id=blob_id,
            registration=registration,
        )

    signer_public_keys = [
        current_committee.members[position].public_key
        for position in current_certificate.signer_positions
    ]
    if not verify_certificate(
        certificate=current_certificate, public_keys=signer_public_keys
    ):
        raise CertifyTransactionError(
            message=(
                "Local certificate verification failed -- the aggregate "
                "signature does not verify against the committee public "
                "keys for its signer positions; refusing to submit Tx2"
            ),
            stage="certify",
        )

    certify_tx2_start = time.monotonic()
    try:
        result = await execute_certify(
            client=client,
            registration=registration,
            certificate=current_certificate,
            package_id=package_id,
            system_object=system_object,
            sender=sender,
            sponsor=sponsor,
            recipient=recipient,
        )
    except (RuntimeError, ValueError) as exc:
        raise CertifyTransactionError(
            message=str(exc),
            stage="certify",
            duration=time.monotonic() - certify_tx2_start,
        ) from exc
    finally:
        certify_tx2_duration = time.monotonic() - certify_tx2_start
    return NativeBlobReceipt(
        blob_id=blob_id_to_url_base64(blob_id=blob_id),
        object_id=result.object_id,
        certified=result.certified,
        end_epoch=registration.end_epoch,
        failed_stage=None,
        timings=dataclasses.replace(
            incoming_timings, certify_tx2=certify_tx2_duration
        ),
    )


async def store_blob_native(
    *,
    client: WalrusClient,
    data: bytes,
    epochs: int,
    deletable: bool = False,
    max_confirmation_requests: int = 64,
    wait_millis: int | None = None,
    sender: str | None = None,
    sponsor: str | None = None,
    payment_coin: str | None = None,
    recipient: str | None = None,
) -> NativeBlobReceipt:
    """Run the full native upload pipeline: encode, register, upload, certify.

    Thin compose only -- this orchestrates :func:`~pytusk.core.encoding.encode_blob`,
    :func:`~pytusk.core.system_ops.execute_reserve_and_register`,
    :func:`upload_slivers`, :func:`collect_confirmations` and :func:`certify`
    in the fixed order documented in the module docstring; it contains no
    logic of its own.

    ``client.config.network.system_object`` is the Walrus System object ID
    field defined on ``WalrusNetworkConfig`` in ``pytusk.config.tusk_config``
    (populated for both built-in networks in that module's
    ``_DEFAULT_NETWORKS``), sitting alongside the already-verified
    ``client.config.network.staking_object`` used by
    :meth:`~pytusk.client.walrus_client.WalrusClient.walrus_epoch` and
    :meth:`~pytusk.client.walrus_client.WalrusClient.committee`.

    On any :class:`NativeUploadError` raised after registration has already
    succeeded (i.e. from :func:`upload_slivers`, :func:`collect_confirmations`
    or :func:`certify`), this returns a :class:`NativeBlobReceipt` with
    ``certified=False`` and ``failed_stage`` set, rather than raising --
    ``registration`` already represents WAL spent and a real on-chain Blob
    object, which is worth reporting back rather than discarding into an
    exception. A failure before registration (e.g. from ``encode_blob`` or
    Tx1 itself) has no such state to report and propagates normally.

    :func:`collect_confirmations` is called here with NO ``positions``
    restriction, i.e. its default of querying every committee member --
    see that function's docstring for why a failed sliver upload must not
    be used to pre-exclude a node from confirmation collection.
    ``max_confirmation_requests`` therefore only reaches
    :func:`collect_confirmations` now; :func:`upload_slivers`'s own
    concurrency is governed by its byte-throttle and per-node-connection
    parameters instead (see its docstring), which this compose function
    leaves at their upstream-aligned defaults rather than re-exposing here.

    ``sender``/``sponsor``/``payment_coin`` are threaded straight through to
    :func:`~pytusk.core.system_ops.execute_reserve_and_register` (Tx1); see
    its docstring for their defaulting behaviour. ``recipient`` is
    DELIBERATELY NOT passed to Tx1 -- Tx1 always transfers the newly
    registered ``Blob`` to the resolved sender, because ``certify_blob``
    (Tx2) requires the signer to own the object it mutates. Instead,
    ``recipient`` is threaded into the :func:`certify` (Tx2) call, which
    transfers the ``Blob`` to it in the SAME PTB, immediately after
    certification succeeds -- atomic with certification, and never handing
    the object away before the sender needs to sign for it. Tx2 is
    otherwise still submitted under the active address with no sponsor --
    this compose function does not thread ``sender``/``sponsor`` into
    :func:`certify`.

    Args:
        client (WalrusClient): Client used for every stage.
        data (bytes): The blob content to store.
        epochs (int): Number of epochs ahead to reserve storage for.
        deletable (bool): Whether the stored blob should be deletable.
        max_confirmation_requests (int): Maximum concurrent confirmation
            requests, passed through to :func:`collect_confirmations`.
        wait_millis (int | None): Upper bound in milliseconds for each
            node's confirmation long-poll wait.
        sender (str | None): Address to sign Tx1 as. Defaults to the active
            address when ``None``. Always the owner of the created ``Blob``
            after Tx1 -- see the note above on why ``recipient`` is not
            threaded into Tx1.
        sponsor (str | None): Address to sponsor Tx1's gas as, or ``None``
            for no sponsorship.
        payment_coin (str | None): Object ID of a ``Coin<WAL>`` to use as
            Tx1 payment. When ``None``, one is selected automatically.
        recipient (str | None): Sui address to transfer the certified
            ``Blob`` to, as part of Tx2 (see the note above). Treated as a
            valid Sui address and used verbatim -- NOT validated. When
            ``None``, the ``Blob`` stays with the resolved ``sender``.

    Returns:
        NativeBlobReceipt: The outcome of the upload attempt, with
            ``timings`` (a :class:`StageTimings`) populated for every stage
            that ran -- on both the fully-certified and the partial/failed
            path.
    """
    pipeline_start = time.monotonic()

    committee = await client.committee()

    encode_start = time.monotonic()
    encoded = await asyncio.to_thread(
        functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
    )
    encode_duration = time.monotonic() - encode_start

    system_object = client.config.network.system_object
    staking_object = client.config.network.staking_object
    package_id = await resolve_package_id(client=client, system_object=system_object)

    register_tx1_start = time.monotonic()
    registration = await execute_reserve_and_register(
        client=client,
        encoded=encoded,
        epochs=epochs,
        deletable=deletable,
        package_id=package_id,
        system_object=system_object,
        payment_coin=payment_coin,
        sender=sender,
        sponsor=sponsor,
    )
    register_tx1_duration = time.monotonic() - register_tx1_start

    sliver_upload_duration: float | None = None
    confirmations_duration: float | None = None
    try:
        sliver_upload_start = time.monotonic()
        try:
            await upload_slivers(
                client=client,
                committee=committee,
                encoded=encoded,
            )
        finally:
            sliver_upload_duration = time.monotonic() - sliver_upload_start

        confirmations_start = time.monotonic()
        try:
            certificate = await collect_confirmations(
                client=client,
                committee=committee,
                blob_id=encoded.blob_id,
                registration=registration,
                wait_millis=wait_millis,
                max_confirmation_requests=max_confirmation_requests,
            )
        finally:
            confirmations_duration = time.monotonic() - confirmations_start

        receipt = await certify(
            client=client,
            committee=committee,
            blob_id=encoded.blob_id,
            registration=registration,
            certificate=certificate,
            package_id=package_id,
            system_object=system_object,
            staking_object=staking_object,
            recipient=recipient,
            stage_timings=StageTimings(
                encode=encode_duration,
                register_tx1=register_tx1_duration,
                sliver_upload=sliver_upload_duration,
                confirmations=confirmations_duration,
                certify_tx2=None,
                total=None,
            ),
        )
        total_duration = time.monotonic() - pipeline_start
        return dataclasses.replace(
            receipt,
            timings=dataclasses.replace(receipt.timings, total=total_duration),
        )
    except NativeUploadError as exc:
        total_duration = time.monotonic() - pipeline_start
        return NativeBlobReceipt(
            blob_id=encoded.blob_id_base64,
            object_id=registration.object_id,
            certified=False,
            end_epoch=registration.end_epoch,
            failed_stage=exc.stage,
            timings=StageTimings(
                encode=encode_duration,
                register_tx1=register_tx1_duration,
                sliver_upload=sliver_upload_duration,
                confirmations=confirmations_duration,
                # exc.duration is None for every NativeUploadError raised
                # before Tx2 is attempted (SliverUploadError,
                # ConfirmationCollectionError, EpochMismatchError); only
                # CertifyTransactionError sets it, to the certify_tx2
                # duration certify()'s own try/finally already recorded
                # before re-raising -- see CertifyTransactionError's
                # docstring.
                certify_tx2=exc.duration,
                total=total_duration,
            ),
        )
