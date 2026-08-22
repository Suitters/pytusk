#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared types, errors, and constants used by two or more native upload
pipeline stages.

See :mod:`pytusk.core.native_upload` (the package's ``__init__.py``) for the
full native upload pipeline description -- stage ordering, why storage nodes
require metadata before slivers, and the four different 32-byte identifier
encodings in play across native upload.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from pysui import SuiCommand, SuiRpcResult

from pytusk.commands.walrus_command import WalrusCommand


class _ExecuteOnlyClient(Protocol):  # noqa: PYI046 -- consumed by sibling modules (fanout.py, confirm.py, certify.py), which ruff's single-file check can't see
    """Structural type for functions that only need ``execute()`` off a
    client -- lets tests pass a minimal fake without casting it to the
    concrete :class:`~pytusk.client.walrus_client.WalrusClient`."""

    async def execute(
        self,
        *,
        command: WalrusCommand | SuiCommand,
        timeout: float | None = None,
        headers: dict | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult: ...


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
