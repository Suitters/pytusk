#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Read a Blob object's on-chain metadata (Walrus "attribute") dynamic field.

Mirrors :func:`~pytusk.core.chain.committee.fetch_staking_inner` in shape:
one ``GetDynamicFields`` call against the owning object, a list-and-filter
over the raw entries (no single-key fetch-by-name exists in pysui's gRPC
layer), and a single :func:`~pytusk.core.chain.committee.protobuf_json_to_python`
decode. Unlike the staking read, a Blob with no metadata set at all is a
NORMAL outcome -- most blobs never call ``insert_or_update_metadata_pair`` --
so a missing dynamic field returns ``None`` here rather than raising.

Chain access is expressed via the :class:`~pytusk.core.chain.committee.ChainReader`
protocol re-exported from :mod:`pytusk.core.chain.committee`, not a concrete
client, for the same reason ``committee.py`` does: this module has no
dependency on ``WalrusClient`` and must not gain one, or
``client/walrus_client.py`` importing from it would become a real import
cycle (see that module's own docstring).
"""

import logging

from pysui import GetDynamicFields

from pytusk.core.chain.committee import ChainReader, JsonValue, protobuf_json_to_python
from pytusk.core.types.blob_metadata import BlobMetadata, Metadata

__all__ = ["fetch_blob_metadata"]

_METADATA_DYNAMIC_FIELD_NAME: str = "metadata"
"""On-chain dynamic field name Walrus uses for a Blob's metadata (Move
``METADATA_DF = b"metadata"``, ``blob.move``)."""

_METADATA_TYPE_SUFFIX: str = "::metadata::Metadata"
"""Move type suffix identifying the metadata dynamic field's value type."""

_logger = logging.getLogger(__name__)


async def fetch_blob_metadata(
    *, reader: ChainReader, blob_object: str
) -> BlobMetadata | None:
    """Fetch and decode a Blob object's metadata dynamic field, if any.

    Args:
        reader (ChainReader): Chain transport to read through.
        blob_object (str): Object ID of the Walrus Blob.

    Returns:
        BlobMetadata | None: The blob's metadata key/value pairs, or
        ``None`` if the blob has no ``metadata`` dynamic field at all --
        the common case for a blob that never called
        ``insert_or_update_metadata_pair``.

    Raises:
        RuntimeError: If the dynamic fields cannot be fetched.
        TypeError: If a matching dynamic field's decoded value is not
            shaped as expected.
    """
    result = await reader.execute_for_all(
        command=GetDynamicFields(object_id=blob_object)
    )
    if not result.is_ok():
        raise RuntimeError(f"Cannot get Blob dynamic fields: {result.result_string}")
    dynamic_fields = result.result_data.dynamic_fields
    _logger.info(
        "GetDynamicFields blob_object=%s: fetched %d raw dynamic field(s) total "
        "(all pages accumulated by pysui execute_for_all)",
        blob_object,
        len(dynamic_fields),
    )
    matching = [
        candidate
        for candidate in dynamic_fields
        if (candidate.value_type or "").endswith(_METADATA_TYPE_SUFFIX)
    ]
    if not matching:
        _logger.info(
            "GetDynamicFields blob_object=%s: no dynamic field ending in %r "
            "found among %d raw entries; blob has no metadata set",
            blob_object,
            _METADATA_TYPE_SUFFIX,
            len(dynamic_fields),
        )
        return None
    selected = matching[0]
    decoded = protobuf_json_to_python(value=selected.field_object.json)
    field = _require_dict(node=decoded, path=_METADATA_DYNAMIC_FIELD_NAME)
    metadata_struct = _require_dict(node=field.get("value"), path="Metadata")
    vec_map = _require_dict(
        node=metadata_struct.get("metadata"), path="Metadata.metadata"
    )
    entries = _require_list(node=vec_map.get("contents"), path="VecMap.contents")
    pairs: list[Metadata] = []
    for index, raw_entry in enumerate(entries):
        entry = _require_dict(node=raw_entry, path=f"VecMap.contents[{index}]")
        pairs.append(
            Metadata(
                key=_require_key_or_value(node=entry.get("key"), path="entry.key"),
                value=_require_key_or_value(
                    node=entry.get("value"), path="entry.value"
                ),
            )
        )
    return BlobMetadata(data=pairs)


def _require_dict(*, node: JsonValue, path: str) -> dict[str, JsonValue]:
    """Return ``node`` as a mapping or raise with the on-chain path.

    Local to this module rather than imported from
    :mod:`pytusk.core.chain.committee`, which keeps its identically-named
    helper module-private -- reaching across that boundary would be an
    encapsulation violation, not reuse.

    Args:
        node (JsonValue): Decoded value to check.
        path (str): Human-readable location, used in the error message.

    Returns:
        dict[str, JsonValue]: The mapping.

    Raises:
        TypeError: If ``node`` is not a mapping.
    """
    if not isinstance(node, dict):
        raise TypeError(f"Expected a mapping at {path}, got {type(node).__name__}")
    return node


def _require_list(*, node: JsonValue, path: str) -> list[JsonValue]:
    """Return ``node`` as a list or raise with the on-chain path.

    Args:
        node (JsonValue): Decoded value to check.
        path (str): Human-readable location, used in the error message.

    Returns:
        list[JsonValue]: The list.

    Raises:
        TypeError: If ``node`` is not a list.
    """
    if not isinstance(node, list):
        raise TypeError(f"Expected a list at {path}, got {type(node).__name__}")
    return node


def _require_key_or_value(*, node: JsonValue, path: str) -> str:
    """Return ``node`` as a string or raise with the on-chain path.

    Args:
        node (JsonValue): Decoded value to check.
        path (str): Human-readable location, used in the error message.

    Returns:
        str: The string value.

    Raises:
        TypeError: If ``node`` is not a string.
    """
    if not isinstance(node, str):
        raise TypeError(f"Expected a string at {path}, got {type(node).__name__}")
    return node
