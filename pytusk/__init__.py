#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pytusk package."""

import logging

# Client
from pytusk.client.walrus_client import WalrusClient, get_walrus_epoch

# Storage-node commands
from pytusk.commands.node_commands import (
    GetStorageConfirmation,
    MetadataAck,
    PutMetadata,
    PutSliver,
    SignedConfirmation,
    SliverAck,
    error_reason,
)

# Read commands
from pytusk.commands.read_commands import (
    ConcatBlobs,
    ReadBlob,
    ReadBlobByObjectId,
    ReadBlobPartial,
    ReadQuiltPatch,
)

# Command base and response types
from pytusk.commands.walrus_command import (
    BlobData,
    BlobReceipt,
    BlobSlice,
    QuiltPatch,
    QuiltReceipt,
    WalrusCommand,
)

# Write commands
from pytusk.commands.write_commands import (
    StoreBlob,
    StoreQuilt,
)

# Configuration
from pytusk.config.tusk_config import (
    NetworkType,
    PytuskConfigModel,
    PytuskConfiguration,
    WalrusNetworkConfig,
)

# Certification (certify_blob wire-format: bitmap, quorum, verify, aggregate)
from pytusk.core.certification import (
    Certificate,
    ConfirmationMismatchError,
    InvalidConfirmationError,
    NodeConfirmation,
    QuorumNotReachedError,
    build_certificate,
    confirmation_message,
    is_quorum,
    min_weight_for_quorum,
    verify_certificate,
    verify_confirmation,
)

# Committee
from pytusk.core.committee import (
    ChainReader,
    WalrusCommittee,
    WalrusCommitteeMember,
    fetch_committee,
    fetch_epoch,
    pack_signers_bitmap,
    unpack_signers_bitmap,
)

# Encoding (RedStuff, blob-ID and root-hash conversions)
from pytusk.core.encoding import (
    RS2_ENCODING_TYPE,
    RS2_MAX_SYMBOL_SIZE,
    RS2_REQUIRED_ALIGNMENT,
    BlobTooLargeError,
    EncodedBlob,
    blob_id_from_url_base64,
    blob_id_to_u256,
    blob_id_to_url_base64,
    decode_standard_base64,
    encode_blob,
    max_blob_size,
    max_n_faulty,
    min_n_correct,
    root_hash_to_u256,
    source_symbol_counts,
    symbol_size,
)

# Native upload orchestration (sliver fan-out, confirmation collection,
# certification, thin end-to-end compose)
from pytusk.core.native_upload import (
    CertifyTransactionError,
    ConfirmationCollectionError,
    EpochMismatchError,
    FanoutReport,
    NativeBlobReceipt,
    NativeUploadError,
    NativeUploadState,
    NodeUploadOutcome,
    SliverUploadError,
    StageTimings,
    assert_certificate_epoch_current,
    certify,
    collect_confirmations,
    object_id_to_raw_bytes,
    store_blob_native,
    upload_slivers,
)

# System operations (Tx1 reserve_space+register_blob and Tx2 certify_blob
# PTB composition; the caller owns the transaction lifecycle -- see
# pytusk.core.system_ops's module docstring)
from pytusk.core.system_ops import (
    CertifyResult,
    Registration,
    add_certify,
    add_reserve_and_register,
    execute_certify,
    execute_reserve_and_register,
    find_created_object_id,
    resolve_package_id,
    select_wal_payment_coin,
)
from pytusk.version import __version__

# pytusk is a LIBRARY: it only emits log records, never configures
# handlers, levels, or any other global logging state. A NullHandler on
# the top-level "pytusk" logger is the standard library idiom for this --
# it silences Python's "No handlers could be found for logger pytusk"
# warning for an SDK user who configures nothing, without imposing any
# output of pytusk's own choosing. Whoever codes to this SDK (e.g. the
# tusky CLI in pytusk/tusky/, or any other application) owns handler
# setup, destinations, and rotation for their own process.
logging.getLogger(__name__).addHandler(logging.NullHandler())

# __all__ is deliberately grouped by source module (mirrors the import blocks
# above) rather than alphabetically sorted; ruff's auto-fix would flatten it
# into one global alpha sort and scatter these grouping comments onto
# unrelated entries.
__all__ = [  # noqa: RUF022
    "__version__",
    # Configuration
    "NetworkType",
    "WalrusNetworkConfig",
    "PytuskConfigModel",
    "PytuskConfiguration",
    # Client
    "WalrusClient",
    "get_walrus_epoch",
    # Committee
    "ChainReader",
    "WalrusCommittee",
    "WalrusCommitteeMember",
    "fetch_committee",
    "fetch_epoch",
    "pack_signers_bitmap",
    "unpack_signers_bitmap",
    # Command base and response types
    "WalrusCommand",
    "BlobData",
    "BlobSlice",
    "QuiltPatch",
    "BlobReceipt",
    "QuiltReceipt",
    # Read commands
    "ReadBlob",
    "ReadBlobPartial",
    "ReadBlobByObjectId",
    "ReadQuiltPatch",
    "ConcatBlobs",
    # Write commands
    "StoreBlob",
    "StoreQuilt",
    # Storage-node commands
    "PutSliver",
    "PutMetadata",
    "GetStorageConfirmation",
    "SliverAck",
    "MetadataAck",
    "SignedConfirmation",
    "error_reason",
    # Encoding
    "RS2_ENCODING_TYPE",
    "RS2_MAX_SYMBOL_SIZE",
    "RS2_REQUIRED_ALIGNMENT",
    "BlobTooLargeError",
    "EncodedBlob",
    "blob_id_from_url_base64",
    "blob_id_to_u256",
    "blob_id_to_url_base64",
    "decode_standard_base64",
    "encode_blob",
    "max_blob_size",
    "max_n_faulty",
    "min_n_correct",
    "root_hash_to_u256",
    "source_symbol_counts",
    "symbol_size",
    # Certification
    "Certificate",
    "ConfirmationMismatchError",
    "InvalidConfirmationError",
    "NodeConfirmation",
    "QuorumNotReachedError",
    "build_certificate",
    "confirmation_message",
    "is_quorum",
    "min_weight_for_quorum",
    "verify_certificate",
    "verify_confirmation",
    # System operations
    "CertifyResult",
    "Registration",
    "add_certify",
    "add_reserve_and_register",
    "execute_certify",
    "execute_reserve_and_register",
    "find_created_object_id",
    "resolve_package_id",
    "select_wal_payment_coin",
    # Native upload orchestration
    "CertifyTransactionError",
    "ConfirmationCollectionError",
    "EpochMismatchError",
    "FanoutReport",
    "NativeBlobReceipt",
    "NativeUploadError",
    "NativeUploadState",
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
