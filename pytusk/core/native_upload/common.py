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

from typing import Protocol

from pysui import SuiCommand, SuiRpcResult

from pytusk.commands.walrus_command import WalrusCommand
from pytusk.core.encoding import (
    object_id_to_raw_bytes,  # noqa: F401 -- back-compat re-export; moved to pytusk.core.encoding
)


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


# StageTimings, NativeBlobReceipt, NativeUploadError, SliverUploadError,
# ConfirmationCollectionError, EpochMismatchError, and CertifyTransactionError
# moved to pytusk.core.types (see pytusk.core.types.receipts and
# pytusk.core.types.errors). Sibling modules in this package import them
# from there directly.
