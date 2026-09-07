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
:func:`~pytusk.core.native_upload.fanout._upload_node`, which PUTs a node's
``PutMetadata`` before any of that node's sliver PUTs and abandons the node
entirely (no sliver PUT attempted) if the metadata PUT fails. This package
owns everything AFTER encoding (:mod:`pytusk.core.encoding`) and Tx1
(:mod:`pytusk.core.ops.blob_execute`): the per-node metadata+sliver fan-out (see
:mod:`pytusk.core.native_upload.fanout`), the confirmation quorum collection
(see :mod:`pytusk.core.native_upload.confirm`), and the Tx2 submission (see
:mod:`pytusk.core.native_upload.certify`). The thin end-to-end compose
function that runs all of it in order lives ABOVE this package, in
:mod:`pytusk.core.pipelines.write` -- this package owns the stages, not
their orchestration, and a function that reaches across two peer packages
cannot sit inside one of them without inverting the dependency between them.
Shared types, errors, and constants used by two or more stages live in
:mod:`pytusk.core.native_upload.common`.

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
  :class:`~pytusk.commands.node_commands.ReadStorageConfirmation`)
- object ID, 32 bytes -> 32 RAW bytes (see :func:`object_id_to_raw_bytes`;
  used inside the signed confirmation message, never in a URL)
"""

from pytusk.core.native_upload.certify import assert_certificate_epoch_current, certify
from pytusk.core.native_upload.confirm import collect_confirmations
from pytusk.core.native_upload.fanout import (
    FanoutReport,
    NodeUploadOutcome,
    upload_slivers,
)
from pytusk.core.types import (
    CertifyTransactionError,
    ConfirmationCollectionError,
    EpochMismatchError,
    NativeBlobReceipt,
    NativeUploadError,
    SliverUploadError,
    StageTimings,
)

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
    "upload_slivers",
]
