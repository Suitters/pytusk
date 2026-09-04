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

# _ExecuteOnlyClient moved to pytusk.core.types (see
# pytusk.core.types.protocols.ExecuteOnlyClient), shedding its leading
# underscore since it is consumed by sibling modules (fanout.py, confirm.py,
# certify.py) rather than being private to this module.


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
