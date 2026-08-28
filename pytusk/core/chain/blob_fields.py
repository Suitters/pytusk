#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Field extraction from an already-fetched Walrus ``Blob`` object.

All three functions below read the JSON view of an object the caller has
already fetched. None take a client and none perform I/O, which is what
keeps them here rather than one layer up in :mod:`pytusk.core.ops` -- the
same rule that places :mod:`pytusk.core.chain.effects` in this package.

Each converts ``obj.json`` exactly once, via
:func:`~pytusk.core.chain.committee.protobuf_json_to_python`, through the
module-private :func:`_converted_fields` helper, rather than hand-walking
``.struct_value.fields`` per hop with its own None checks at every level.
This module is also the only place under :mod:`pytusk.tusky` and
:mod:`pytusk.core` that touches ``struct_value`` directly for Blob object
fields -- ``pytusk.tusky.tusky_cmds_query`` (``blobs()``) and
``pytusk.tusky.tusky_cmds_native_upload`` used to hand-walk it themselves;
both were repointed onto this module at Plan #28 step 12.

A MISSING FIELD IS AN ERROR, NOT A DEFAULT. Every failure below raises
``ValueError`` rather than returning a zero or ``None`` that a caller could
mistake for a real value. The distinction matters most on
:func:`blob_certified_epoch`, where a blob that is genuinely registered but
not yet certified has a field that is PRESENT AND NULL -- reported as
``None`` -- while a response missing the field entirely means the read
itself was incomplete. Collapsing those two into one answer would let an
incomplete RPC response read as "not certified yet". This is why
:func:`_converted_fields` returns a plain ``dict`` rather than something
that collapses "key absent" and "key present with value None" into one
falsy check -- callers that need the distinction test key membership
(``"x" not in fields``), never truthiness.

These moved out of ``pytusk.tusky.tusky_cmds_common`` at Plan #28 step 10,
under the placement rule: the consumer determines the module, and parsing a
Blob object is something an SDK user fetching Blobs wants as much as the CLI
does. They already raised rather than exiting, so the move is a relocation
and not a change of contract.
"""

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot

from pytusk.core.chain.committee import JsonValue, protobuf_json_to_python

__all__ = [
    "blob_certified_epoch",
    "blob_deletable_and_end_epoch",
    "blob_id_from_object",
]


def _converted_fields(
    *, obj: sui_prot.Object, missing_json_detail: str
) -> dict[str, JsonValue]:
    """Convert a fetched object's JSON view into a plain field mapping.

    Calls :func:`~pytusk.core.chain.committee.protobuf_json_to_python` once
    on ``obj.json``, so every function in this module indexes plain Python
    afterward instead of hopping ``.struct_value.fields`` by hand. Shared by
    :func:`blob_deletable_and_end_epoch`, :func:`blob_certified_epoch`, and
    :func:`blob_id_from_object` -- all three need the same top-level field
    mapping and differ only in what they read out of it.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.
        missing_json_detail (str): Fragment naming what the caller could not
            determine, folded into the "no JSON view" error message so each
            public function in this module keeps its own wording.

    Returns:
        dict[str, JsonValue]: The object's top-level fields, as plain Python.
            A field that is present but explicitly null converts to a
            ``None`` entry, distinct from the key being absent altogether.

    Raises:
        ValueError: If the object has no JSON view (``obj.json`` unset, or
            its ``struct_value`` unset) -- an incomplete RPC response.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id} has no JSON view; cannot determine "
            f"{missing_json_detail}."
        )
    converted = protobuf_json_to_python(value=obj.json)
    # obj.json.struct_value being truthy above guarantees the struct_value
    # branch of protobuf_json_to_python fires, so this is always a dict.
    assert isinstance(converted, dict)
    return converted


def blob_deletable_and_end_epoch(*, obj: sui_prot.Object) -> tuple[bool, int]:
    """Extract a Blob object's deletable flag and storage end_epoch.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.

    Returns:
        tuple[bool, int]: (deletable, end_epoch).

    Raises:
        ValueError: If the object's JSON view is missing fields a Walrus
            Blob object is expected to have (e.g. an incomplete RPC
            response). This is distinct from a normal blob with a real
            deletable/end_epoch value and must not be silently treated as
            "not eligible" by callers.
    """
    fields = _converted_fields(obj=obj, missing_json_detail="deletable/end_epoch")
    storage_val = fields.get("storage")
    if not isinstance(storage_val, dict):
        # ValueError, not TypeError -- this module's contract (see the
        # module docstring) is ValueError for every malformed-object case,
        # a missing/wrong-shaped 'storage' field included.
        raise ValueError(  # noqa: TRY004
            f"Object {obj.object_id} is missing its 'storage' field; "
            "cannot determine end_epoch."
        )
    if "end_epoch" not in storage_val or storage_val["end_epoch"] is None:
        raise ValueError(
            f"Object {obj.object_id}'s storage field is missing 'end_epoch'."
        )
    end_epoch = int(storage_val["end_epoch"])  # type: ignore[arg-type]
    if "deletable" not in fields:
        raise ValueError(f"Object {obj.object_id} is missing its 'deletable' field.")
    deletable = bool(fields["deletable"])
    return deletable, end_epoch


def blob_certified_epoch(*, obj: sui_prot.Object) -> int | None:
    """Extract a Blob object's certified_epoch, if the blob has been certified.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.

    Returns:
        int | None: The epoch the blob was certified in, or None if the
            blob has been registered but not yet certified by storage nodes.

    Raises:
        ValueError: If the object's JSON view is missing the certified_epoch
            field entirely (e.g. an incomplete RPC response). Distinct from
            a normal uncertified blob, whose certified_epoch field is
            present but null, and must not be silently treated the same.
    """
    fields = _converted_fields(obj=obj, missing_json_detail="certified_epoch")
    if "certified_epoch" not in fields:
        raise ValueError(
            f"Object {obj.object_id} is missing its 'certified_epoch' field."
        )
    certified_epoch = fields["certified_epoch"]
    if certified_epoch is None:
        return None
    return int(certified_epoch)  # type: ignore[arg-type]


def blob_id_from_object(*, obj: sui_prot.Object) -> bytes:
    """Extract a Blob object's raw 32-byte Walrus blob ID.

    The on-chain ``Blob.blob_id`` is a Move ``u256``, surfaced in the JSON
    view as a decimal string. This converts it to raw bytes the same way
    :func:`~pytusk.core.encoding.blob_id_to_u256` converts the other
    direction (little-endian). Raw bytes are the canonical form both other
    representations derive from: a caller wanting the URL-safe base64 form
    used in storage-node paths calls
    :func:`~pytusk.core.encoding.blob_id_to_url_base64` on the result; a
    caller wanting the ``u256`` int calls
    :func:`~pytusk.core.encoding.blob_id_to_u256` on it. Returning the
    decimal string itself instead would leave that identical int/bytes
    conversion duplicated at every call site, which is what this function
    exists to remove.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.

    Returns:
        bytes: The raw 32-byte blob ID.

    Raises:
        ValueError: If the object's JSON view is missing its 'blob_id'
            field (e.g. an incomplete RPC response).
    """
    fields = _converted_fields(obj=obj, missing_json_detail="blob_id")
    blob_id_val = fields.get("blob_id")
    if not isinstance(blob_id_val, str) or not blob_id_val:
        raise ValueError(f"Object {obj.object_id} is missing its 'blob_id' field.")
    return int(blob_id_val).to_bytes(32, byteorder="little")
