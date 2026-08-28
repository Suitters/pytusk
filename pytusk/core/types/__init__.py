#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared result, outcome, error, and tip-config types.

A LEAF package: nothing here imports from
:mod:`pytusk.core.native_upload`, :mod:`pytusk.core.relay_upload`, or
:mod:`pytusk.commands` -- both upload pipelines, and the transport layer
those pipelines drive, import types from here instead. That direction is
what dissolves the import cycle that previously required
``pytusk.core.relay_types`` to live outside :mod:`pytusk.core.relay_upload`.
"""

from pytusk.core.types.errors import (
    CertifyTransactionError,
    ConfirmationCollectionError,
    ConfirmationMismatchError,
    EpochMismatchError,
    InvalidConfirmationError,
    NativeUploadError,
    QuorumNotReachedError,
    RegistrationPendingError,
    RelayCertificateParseError,
    RelayCertifyTransactionError,
    RelayUploadError,
    SliverUploadError,
    TipCeilingExceededError,
    TipConfigError,
    TipPaymentError,
)
from pytusk.core.types.outcomes import RelayOutcome, RelayUploadOutcome
from pytusk.core.types.protocols import (
    ExecuteOnlyClient,
    StageTimingsProtocol,
    UploadReceipt,
)
from pytusk.core.types.receipts import (
    CertifyResult,
    NativeBlobReceipt,
    Registration,
    RelayBlobReceipt,
    RelayStageTimings,
    RelayUploadResult,
    SplitResult,
    StageTimings,
    StorageObject,
    StorageOpResult,
)
from pytusk.core.types.tips import (
    FROM_GAS,
    AuthPackage,
    ConstTip,
    LinearTip,
    TipComposition,
    TipConfig,
    TipKind,
    TipQuote,
    TipResult,
)

__all__ = [
    "FROM_GAS",
    "AuthPackage",
    "CertifyResult",
    "CertifyTransactionError",
    "ConfirmationCollectionError",
    "ConfirmationMismatchError",
    "ConstTip",
    "EpochMismatchError",
    "ExecuteOnlyClient",
    "InvalidConfirmationError",
    "LinearTip",
    "NativeBlobReceipt",
    "NativeUploadError",
    "QuorumNotReachedError",
    "Registration",
    "RegistrationPendingError",
    "RelayBlobReceipt",
    "RelayCertificateParseError",
    "RelayCertifyTransactionError",
    "RelayOutcome",
    "RelayStageTimings",
    "RelayUploadError",
    "RelayUploadOutcome",
    "RelayUploadResult",
    "SliverUploadError",
    "SplitResult",
    "StageTimings",
    "StageTimingsProtocol",
    "StorageObject",
    "StorageOpResult",
    "TipCeilingExceededError",
    "TipComposition",
    "TipConfig",
    "TipConfigError",
    "TipKind",
    "TipPaymentError",
    "TipQuote",
    "TipResult",
    "UploadReceipt",
]
