#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Weighted committee fan-out choreography.

ONE implementation of the dispatch-wait-threshold-grace-cancel-clean-up
sequence that every committee-wide Walrus operation performs. Sliver upload
and confirmation collection each grew their own copy of it; the second copy
was written by deliberately mirroring the first, and its comments say so.
A third copy is where that stops being tolerable, so the choreography lives
here once.

WHAT THIS MODULE OWNS: the async control flow only -- waiting on the next
completion, testing the caller's threshold, sizing and running the grace
window, cancelling stragglers, and guaranteeing no task outlives the call.
That is the subtle, correctness-critical part, and the part where a fix
applied to one copy silently failed to reach the others.

WHAT IT DELIBERATELY DOES NOT OWN: what a result MEANS. Callers classify
their own outcomes, keep their own accumulators, and do their own weight
arithmetic. The two existing callers disagree on this and are both right --
sliver upload records every node including failures and cancellations,
while confirmation collection keeps only usable confirmations and merely
logs the rest. A shared return type would force one of them to accept
records it discards. Hence the callbacks: nothing crosses this seam that
the receiving caller did not ask for.

Per-node concerns -- semaphores, byte throttles, retry and backoff -- stay
inside the caller's own coroutine, which is why this function takes tasks
that are ALREADY created rather than a factory. Retry in particular is NOT
uniform across callers: sliver upload retries each PUT with jittered
exponential backoff, confirmation collection does not retry at all. Pulling
retry in here would silently give the confirmation path a behaviour it has
never had.

LAYERING: this module takes no client of any kind. It sits below the
``pytusk/client/`` seam and must stay there.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from typing import TypeVar

from pytusk.core.chain.committee import WalrusCommitteeMember

_logger = logging.getLogger(__name__)

__all__ = ["fan_out_to_committee"]

T = TypeVar("T")


async def fan_out_to_committee(
    *,
    tasks: Mapping[asyncio.Task[T], WalrusCommitteeMember],
    on_completed: Callable[[asyncio.Task[T], WalrusCommitteeMember], None],
    on_cancelled: Callable[[asyncio.Task[T], WalrusCommitteeMember], None],
    should_stop: Callable[[], bool],
    label: str,
    on_threshold_reached: Callable[[float], None] | None = None,
    grace_base_seconds: float = 0.5,
    grace_factor: float = 0.5,
) -> None:
    """Drive an already-dispatched committee fan-out to its threshold.

    Every task in ``tasks`` is assumed to be already running. This function
    waits on them, folding each completion through ``on_completed`` as it
    lands, and stops waiting as soon as ``should_stop`` reports the caller's
    threshold met -- rather than always waiting for every node.

    Once the threshold is met, stragglers still in flight get a grace window
    of ``grace_base_seconds + grace_factor * time_to_threshold``: work
    already paid for is cheap to collect, but the window scales with how
    long the threshold itself took, so a slow run does not wait a fixed
    eternity. Whatever is still pending when the window closes is cancelled
    and reported through ``on_cancelled``.

    ROBUSTNESS INVARIANT: no task in ``tasks`` outlives this call, on ANY
    exit path -- normal completion, threshold failure, or an exception
    propagating (including this function's own task being cancelled from
    outside). The ``finally`` block checks every task it was given rather
    than a loop-local name, since those may not be bound yet if an exception
    lands early. A leftover task would otherwise run on detached, still
    holding whatever per-node reservations its coroutine took, and
    eventually raise once a caller further up tears down a single-use
    client.

    Args:
        tasks (Mapping[asyncio.Task[T], WalrusCommitteeMember]): The
            already-created per-node tasks, each mapped to the committee
            member it addresses. The member is passed back to the callbacks
            and used for straggler log lines.
        on_completed (Callable[[asyncio.Task[T], WalrusCommitteeMember], None]):
            Invoked for each task that finishes, in both the main wait and
            the grace window. Receives the finished task so the caller can
            apply its own exception handling and classification. Must not
            raise.
        on_cancelled (Callable[[asyncio.Task[T], WalrusCommitteeMember], None]):
            Invoked for each straggler cancelled when the grace window
            closes, AFTER ``task.cancel()``. Callers that record failures
            record them here; callers that only log, log here. Must not
            raise.
        should_stop (Callable[[], bool]): The caller's threshold predicate,
            over the caller's own accumulated state. Called after each batch
            of completions, and once more after the wait loop ends. Must be
            cheap and free of side effects.
        label (str): Operation name for log lines, e.g. ``"upload_slivers"``.
            Log text is deliberately identical to what each caller emitted
            before this choreography was shared, so existing log-based
            diagnostics keep working unchanged.
        on_threshold_reached (Callable[[float], None] | None): Invoked with
            the elapsed seconds when ``should_stop`` reports the threshold
            met. Optional because only the caller knows its own weights, so
            the caller -- not this function -- formats that log line.
        grace_base_seconds (float): Fixed part of the straggler window.
        grace_factor (float): Fraction of time-to-threshold added to it.

    Returns:
        None: Every result reaches the caller through its callbacks. There
        is deliberately no return value to widen as callers are added.
    """
    start_time = time.monotonic()
    pending: set[asyncio.Task[T]] = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                on_completed(task, tasks[task])
            if should_stop():
                break

        if should_stop() and on_threshold_reached is not None:
            on_threshold_reached(time.monotonic() - start_time)

        # If the threshold was never met, `pending` is already empty here --
        # the loop above only exits early on the threshold, otherwise it
        # runs until every task is done -- so this whole block is a no-op on
        # the failure path.
        if pending:
            time_to_threshold = time.monotonic() - start_time
            extra_time = grace_base_seconds + grace_factor * time_to_threshold
            _logger.info(
                "%s grace window: extra_time=%.1fs pending_nodes=%d",
                label,
                extra_time,
                len(pending),
            )
            done, still_pending = await asyncio.wait(pending, timeout=extra_time)
            for task in done:
                on_completed(task, tasks[task])
            for task in still_pending:
                member = tasks[task]
                task.cancel()
                _logger.info(
                    "%s cancelled straggler: node_id=%s", label, member.node_id
                )
                on_cancelled(task, member)
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
    finally:
        leftover = [task for task in tasks if not task.done()]
        if leftover:
            for task in leftover:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*leftover, return_exceptions=True)
