#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Field extraction from an already-fetched Walrus ``Blob`` object.

Both helpers read the JSON view of an object the caller has already
fetched. Neither takes a client and neither performs I/O, which is what
keeps them here rather than one layer up in :mod:`pytusk.core.ops` -- the
same rule that places :mod:`pytusk.core.chain.effects` in this package.

A MISSING FIELD IS AN ERROR, NOT A DEFAULT. Every failure below raises
``ValueError`` rather than returning a zero or ``None`` that a caller could
mistake for a real value. The distinction matters most on
:func:`blob_certified_epoch`, where a blob that is genuinely registered but
not yet certified has a field that is PRESENT AND NULL -- reported as
``None`` -- while a response missing the field entirely means the read
itself was incomplete. Collapsing those two into one answer would let an
incomplete RPC response read as "not certified yet".

These moved out of ``pytusk.tusky.tusky_cmds_common`` at Plan #28 step 10,
under the placement rule: the consumer determines the module, and parsing a
Blob object is something an SDK user fetching Blobs wants as much as the CLI
does. They already raised rather than exiting, so the move is a relocation
and not a change of contract.
"""

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot

__all__ = [
    "blob_certified_epoch",
    "blob_deletable_and_end_epoch",
]


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
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id} has no JSON view; cannot determine "
            "deletable/end_epoch."
        )
    fields = obj.json.struct_value.fields
    storage_val = fields.get("storage")
    if not (storage_val and storage_val.struct_value):
        raise ValueError(
            f"Object {obj.object_id} is missing its 'storage' field; "
            "cannot determine end_epoch."
        )
    end_epoch_val = storage_val.struct_value.fields.get("end_epoch")
    if end_epoch_val is None:
        raise ValueError(
            f"Object {obj.object_id}'s storage field is missing 'end_epoch'."
        )
    end_epoch = int(end_epoch_val.number_value or 0)
    deletable_val = fields.get("deletable")
    if deletable_val is None:
        raise ValueError(f"Object {obj.object_id} is missing its 'deletable' field.")
    deletable = bool(deletable_val.bool_value)
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
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id} has no JSON view; cannot determine "
            "certified_epoch."
        )
    certified_epoch_val = obj.json.struct_value.fields.get("certified_epoch")
    if certified_epoch_val is None:
        raise ValueError(
            f"Object {obj.object_id} is missing its 'certified_epoch' field."
        )
    if certified_epoch_val.null_value is not None:
        return None
    return int(certified_epoch_val.number_value or 0)
