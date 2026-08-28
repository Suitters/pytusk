#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus upload relay orchestration: tipping, relay upload, and certification.

This package is a PEER of :mod:`pytusk.core.native_upload`, never a
submodule of it. Native upload fans slivers out to every storage node
itself; relay upload hands the blob to a single relay that performs that
fan-out on the client's behalf, which is what makes mainnet writes
practical. Neither package imports the other.

This package owns the relay STAGES. The end-to-end compose function that
runs them in order lives ABOVE it, in :mod:`pytusk.core.pipelines.write`,
alongside its native counterpart -- a function that orchestrates two peer
packages cannot sit inside one of them without inverting the dependency
between them.

The relay upload order is FIXED and must not be rearranged::

    encode -> committee/n_shards -> GET tip-config ->
    [reserve_space + register_blob + tip, ONE PTB] (Tx1) ->
    POST blob-upload-relay -> parse certificate -> certify_blob (Tx2)

Two constraints force that order. The relay REJECTS an upload for an
unregistered blob, so Tx1 must complete before the POST. And the tip is
computed from the ENCODED blob length, which is only known once the
committee's shard count is in hand -- so the committee read precedes the
tip quote, not the other way round.

The registration and the tip share ONE transaction deliberately. The
authentication package must be input 0 of that transaction, and the relay
accepts the encapsulated bundle as a single unit.

FAILURE IS REPORTED, NOT RAISED. A relay upload can leave a tip paid and
the upload unfinished, and that state is recoverable: the relay implements
no replay protection, so re-POSTing the identical ``tx_id`` and ``nonce``
is accepted and never re-charges. :class:`RelayBlobReceipt` therefore
carries a :class:`RelayOutcome` plus the resumption tokens rather than
raising, and pytusk must NEVER state that a tip is lost -- the relay's
freshness threshold is unobservable from the client. Exceptions are
reserved for caller contract violations. Independently of any resumption,
``tusky certify_blob --blobid <id>`` recovers a blob by querying the
committee directly, with no re-POST and no dependence on the tip at all.

Retry policy follows from the same asymmetry: transport errors and 5xx are
retried, 400/401/402 are not. A deterministic refusal will refuse again,
and a fixed paid tip cannot satisfy a 402.
"""

from pytusk.core.relay_upload.common import (
    AuthPackage,
    TipQuote,
    TipResult,
)
from pytusk.core.relay_upload.relay_certify import parse_relay_certificate
from pytusk.core.relay_upload.tip import (
    FROM_GAS,
    add_tip,
    build_auth_package,
    compute_tip,
    execute_tip,
    fetch_tip_config,
    quote_tip,
)
from pytusk.core.relay_upload.upload import (
    DEFAULT_MAX_UPLOAD_ATTEMPTS,
    upload_to_relay,
)
from pytusk.core.types import (
    ConstTip,
    LinearTip,
    RelayBlobReceipt,
    RelayCertificateParseError,
    RelayOutcome,
    RelayStageTimings,
    RelayUploadError,
    RelayUploadOutcome,
    RelayUploadResult,
    TipConfig,
    TipConfigError,
    TipKind,
    TipPaymentError,
)

__all__ = [
    "DEFAULT_MAX_UPLOAD_ATTEMPTS",
    "FROM_GAS",
    "AuthPackage",
    "ConstTip",
    "LinearTip",
    "RelayBlobReceipt",
    "RelayCertificateParseError",
    "RelayOutcome",
    "RelayStageTimings",
    "RelayUploadError",
    "RelayUploadOutcome",
    "RelayUploadResult",
    "TipConfig",
    "TipConfigError",
    "TipKind",
    "TipPaymentError",
    "TipQuote",
    "TipResult",
    "add_tip",
    "build_auth_package",
    "compute_tip",
    "execute_tip",
    "fetch_tip_config",
    "parse_relay_certificate",
    "quote_tip",
    "upload_to_relay",
]
