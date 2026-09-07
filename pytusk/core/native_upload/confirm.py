#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Confirmation-collection stage of the native upload pipeline.

See :mod:`pytusk.core.native_upload` (the package's ``__init__.py``) for the
full native upload pipeline description and stage ordering.
"""

import asyncio
import contextlib
import functools
import logging
import time
from collections.abc import Sequence

from pytusk.commands.node_commands import ReadStorageConfirmation
from pytusk.core.certification import (
    Certificate,
    ConfirmationMismatchError,
    InvalidConfirmationError,
    NodeConfirmation,
    QuorumNotReachedError,
    build_certificate,
    confirmation_message,
    min_weight_for_quorum,
)
from pytusk.core.chain import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.committee_fanout import fan_out_to_committee
from pytusk.core.encoding import object_id_to_raw_bytes
from pytusk.core.native_upload.common import _HEARTBEAT_INTERVAL_SECONDS
from pytusk.core.types import ConfirmationCollectionError, Registration
from pytusk.core.types.protocols import ExecuteOnlyClient

_logger = logging.getLogger(__name__)


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
    client: ExecuteOnlyClient,
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
    # server-side long poll (wait_millis; ReadStorageConfirmation is sent
    # with wait_for_registration=True).
    async with semaphore:
        result = await client.execute(
            command=ReadStorageConfirmation(
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
    client: ExecuteOnlyClient,
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

    Queries ``ReadStorageConfirmation`` against every committee member by
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
    # front, then fan_out_to_committee stops WAITING once confirmed weight
    # reaches quorum rather than waiting for literally every candidate. The
    # choreography -- wait, grace window, straggler cancellation, leftover
    # cleanup -- is shared with upload_slivers rather than mirrored by hand;
    # see pytusk.core.committee_fanout. What stays HERE is what this caller means by a
    # result: only usable confirmations are retained, and everything else is
    # logged and dropped.
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

    def _unusable(*, member: WalrusCommitteeMember, reason: str | None) -> None:
        _logger.warning(
            "Storage node %s (%s) returned no usable confirmation "
            "(reason=%s); tolerated so long as quorum is still met "
            "from the rest",
            member.node_id,
            member.base_url,
            reason,
        )

    def _fold(
        task: asyncio.Task[tuple[NodeConfirmation | None, str | None]],
        member: WalrusCommitteeMember,
    ) -> None:
        nonlocal weight_confirmed
        confirmation, reason = _confirmation_outcome_from_task(
            task=task, member=member, confirm_progress=confirm_progress
        )
        if confirmation is None:
            _unusable(member=member, reason=reason)
        else:
            confirmations.append(confirmation)
            weight_confirmed += confirmation.weight

    def _cancelled(
        _task: asyncio.Task[tuple[NodeConfirmation | None, str | None]],
        member: WalrusCommitteeMember,
    ) -> None:
        _unusable(member=member, reason="cancelled")

    def _quorum_reached(elapsed: float) -> None:
        _logger.info(
            "collect_confirmations quorum reached: weight=%d/%d elapsed=%.1fs",
            weight_confirmed,
            required_weight,
            elapsed,
        )

    try:
        await fan_out_to_committee(
            tasks=task_members,
            on_completed=_fold,
            on_cancelled=_cancelled,
            should_stop=lambda: weight_confirmed >= required_weight,
            label="collect_confirmations",
            on_threshold_reached=_quorum_reached,
            grace_base_seconds=grace_base_seconds,
            grace_factor=grace_factor,
        )
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

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
