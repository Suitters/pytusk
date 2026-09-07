#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Public surface of the encoding package: RedStuff blob encoding plus
Sui object-ID and Walrus blob-ID/root-hash byte conversions.

:mod:`pytusk.core.encoding.redstuff` holds the RedStuff erasure-coding size
math, the blob-ID/root-hash <-> base64/u256 conversions, and
:func:`~pytusk.core.encoding.redstuff.encode_blob` itself -- one coherent
module, since every symbol in it exists to serve RedStuff encoding.
:mod:`pytusk.core.encoding.object_ids` holds
:func:`~pytusk.core.encoding.object_ids.object_id_to_raw_bytes`, kept in its
own module because it operates on a DIFFERENT identifier space (Sui object
IDs, not Walrus blob IDs or root hashes) and was previously misfiled in
``pytusk.core.utils`` despite being pure byte/ID conversion like everything
else here.

Client-free: nothing in this package touches ``WalrusClient`` or the
network.
"""

from pytusk.core.encoding.object_ids import object_id_to_raw_bytes
from pytusk.core.encoding.quilt import (
    QUILT_BLOB_ATTRIBUTES,
    QuiltAssemblyError,
    assemble_quilt,
    quilt_patch_id,
    validate_quilt_identifier,
)
from pytusk.core.encoding.redstuff import (
    RS2_ENCODING_TYPE,
    RS2_MAX_SYMBOL_SIZE,
    RS2_REQUIRED_ALIGNMENT,
    BlobDecodeError,
    BlobTooLargeError,
    EncodedBlob,
    MetadataVerificationError,
    SliverVerificationError,
    VerifiedBlobMetadata,
    blob_id_from_url_base64,
    blob_id_to_u256,
    blob_id_to_url_base64,
    decode_blob,
    decode_standard_base64,
    encode_blob,
    encoded_blob_length,
    max_blob_size,
    max_n_faulty,
    metadata_length,
    min_n_correct,
    pair_index_to_shard_index,
    root_hash_to_u256,
    rotation_offset,
    shard_index_to_pair_index,
    source_symbol_counts,
    symbol_size,
    verify_blob_metadata,
    verify_sliver,
)

__all__ = [
    "QUILT_BLOB_ATTRIBUTES",
    "RS2_ENCODING_TYPE",
    "RS2_MAX_SYMBOL_SIZE",
    "RS2_REQUIRED_ALIGNMENT",
    "BlobDecodeError",
    "BlobTooLargeError",
    "EncodedBlob",
    "MetadataVerificationError",
    "QuiltAssemblyError",
    "SliverVerificationError",
    "VerifiedBlobMetadata",
    "assemble_quilt",
    "blob_id_from_url_base64",
    "blob_id_to_u256",
    "blob_id_to_url_base64",
    "decode_blob",
    "decode_standard_base64",
    "encode_blob",
    "encoded_blob_length",
    "max_blob_size",
    "max_n_faulty",
    "metadata_length",
    "min_n_correct",
    "object_id_to_raw_bytes",
    "pair_index_to_shard_index",
    "quilt_patch_id",
    "root_hash_to_u256",
    "rotation_offset",
    "shard_index_to_pair_index",
    "source_symbol_counts",
    "symbol_size",
    "validate_quilt_identifier",
    "verify_blob_metadata",
    "verify_sliver",
]
