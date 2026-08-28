#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared types and errors used by two or more upload relay pipeline stages.

See :mod:`pytusk.core.relay_upload` (the package's ``__init__.py``) for the
full relay upload description -- stage ordering, why the tip and the
registration must share a single transaction, and why a failed POST is
reported as an outcome rather than raised.
"""

from pytusk.core.types import (  # noqa: F401 -- back-compat re-exports; moved to pytusk.core.types
    AuthPackage,
    TipQuote,
    TipResult,
)

# RelayOutcome moved to pytusk.core.types (see pytusk.core.types.outcomes).
# RelayStageTimings, RelayBlobReceipt, and RelayUploadResult moved to
# pytusk.core.types (see pytusk.core.types.receipts). RelayUploadError,
# TipConfigError, TipPaymentError, and RelayCertificateParseError moved to
# pytusk.core.types (see pytusk.core.types.errors). AuthPackage, TipQuote,
# and TipResult moved to pytusk.core.types (see pytusk.core.types.tips) so
# that pytusk.core.ops.tip_compose.add_tip -- which needs AuthPackage but
# must stay import-cycle-free of pytusk.core.relay_upload -- can reach it;
# re-imported here (rather than re-defined) for backward compatibility,
# since pytusk.core.relay_upload.__init__ and pytusk.core.relay_upload.tip
# both import them from this module.
