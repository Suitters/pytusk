#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""READ layer for Walrus ``Storage`` objects.

Contains :func:`list_storage_objects` (a client-driven query -- composes
no PTB and submits nothing, but does need a client to query owned
objects), and the pure parsers :func:`storage_from_object`,
:func:`storage_from_blob`, and the shared private helper
``_storage_from_field_map`` they both delegate to.

A ``Storage`` object is a raw capacity reservation -- a byte count held for
an epoch range -- and is the resource a ``Blob`` consumes when it is
registered. While a ``Storage`` is embedded in a ``Blob`` it is a WRAPPED
object: its ``UID`` is visible in the blob's contents but the object itself
cannot be fetched or addressed independently. It becomes an ordinary owned
object again only when unwrapped, e.g. by ``system::delete_blob`` (which
returns the storage intact) or by ``system::reserve_space`` /
``storage_resource::split_by_*`` (which create new ones).
"""

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import GetObjectsForType, GetObjectsOwnedByAddress
from pysui.sui.sui_grpc.suimsgs.google import protobuf as pb

from pytusk.client.walrus_client import WalrusClient
from pytusk.config.tusk_config import NetworkType
from pytusk.core.types.receipts import StorageObject

__all__ = [
    "list_storage_objects",
    "storage_from_blob",
    "storage_from_object",
]


async def list_storage_objects(
    *, client: WalrusClient, owner: str, package_id: str
) -> list[StorageObject]:
    """List the standalone ``Storage`` objects owned by ``owner``.

    Neither an ``add_*`` nor an ``execute_*``: this composes no PTB and
    submits nothing. It is a read helper.

    On a PRODUCTION network (mainnet) the Walrus package address is
    stable, so this filters SERVER-SIDE via ``GetObjectsForType`` on
    ``package_id`` -- the node does the filtering, not this function.

    On any other network (e.g. testnet, whose Walrus contracts are
    periodically redeployed under a NEW package address) this instead
    lists EVERY owned object via ``GetObjectsOwnedByAddress`` and filters
    CLIENT-SIDE on the ``::storage_resource::Storage`` suffix of
    ``object_type``. Move type tags are fixed at mint time and never
    change when a package is upgraded, so a ``Storage`` minted under a
    PRIOR package version keeps that version's address in its type tag
    forever. Filtering server-side by the CURRENT ``package_id`` would
    silently miss it -- confirmed live against testnet: ``package_id``
    resolved from ``System.package_id`` reflects the CURRENT package, but
    existing ``Storage``/``Blob`` objects can carry an OLDER package
    address in their type tag, so a ``GetObjectsForType`` filter on the
    current address then matches nothing even though the objects exist.
    The suffix filter is package-address agnostic, so it finds a
    ``Storage`` regardless of which package version minted it.

    ``package_id`` is taken as a parameter rather than resolved here from
    the System object, so it is only actually used on the PRODUCTION path
    above -- ignored otherwise. A caller composing storage operations
    already holds it (every ``add_*``/``execute_*`` in
    :mod:`pytusk.core.ops.storage_compose` /
    :mod:`pytusk.core.ops.storage_execute` requires it), and resolving it
    internally would add a hidden network round trip. Callers that do not
    have it can obtain one via :func:`~pytusk.core.ops.system_reads.resolve_package_id`.

    Only UNWRAPPED storage is returned. A ``Storage`` still embedded in a
    ``Blob`` is a wrapped object with no independent owner record, so it
    does not appear in an owned-object listing at all -- see this module's
    docstring.

    No epoch filtering is applied. ``end_epoch`` may already have passed;
    such a ``Storage`` is still splittable, fusable, and destroyable. To
    show only still-useful reservations, compare
    :attr:`~pytusk.core.types.receipts.StorageObject.end_epoch` against a
    separately fetched current epoch (e.g. ``await client.walrus_epoch()``).

    Args:
        client (WalrusClient): Client used to query owned objects.
        owner (str): Address whose ``Storage`` objects are listed.
        package_id (str): Current Walrus package ID. Used to build the
            ``<package_id>::storage_resource::Storage`` type filter on a
            PRODUCTION network; ignored on any other network, where every
            owned object is scanned instead.

    Returns:
        list[StorageObject]: Every owned ``Storage``, in the order the node
            returned them. Empty when ``owner`` holds none.

    Raises:
        RuntimeError: If the objects cannot be listed.
    """
    if client.config.network.network_type == NetworkType.PRODUCTION:
        result = await client.execute_for_all(
            command=GetObjectsForType(
                owner=owner,
                object_type=f"{package_id}::storage_resource::Storage",
            )
        )
        if not result.is_ok():
            raise RuntimeError(
                f"Cannot list Storage objects for {owner}: {result.result_string}"
            )
        candidates = result.result_data.objects
    else:
        result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
        if not result.is_ok():
            raise RuntimeError(
                f"Cannot list Storage objects for {owner}: {result.result_string}"
            )
        candidates = [
            obj
            for obj in result.result_data.objects
            if obj.object_type
            and obj.object_type.endswith("::storage_resource::Storage")
        ]

    storages: list[StorageObject] = []
    for obj in candidates:
        try:
            storages.append(storage_from_object(obj=obj))
        except ValueError:
            # A listing entry with no JSON view is skipped rather than
            # failing the whole listing. A caller who fetched ONE object by
            # ID gets the ValueError instead, because there a missing view
            # means the specific thing they asked for is unreadable.
            continue
    return storages


def storage_from_object(*, obj: sui_prot.Object) -> StorageObject:
    """Parse a fetched Sui object into a :class:`~pytusk.core.types.receipts.StorageObject`.

    Shared by :func:`list_storage_objects` and by callers holding a single
    ``Storage`` fetched by ID -- notably the client-side pre-flight for a
    fuse, which needs both operands as
    :class:`~pytusk.core.types.receipts.StorageObject` before
    :func:`~pytusk.core.ops.storage_compose.fuse_incompatibility` can judge
    them.

    ``start_epoch`` and ``end_epoch`` are ``u32`` and arrive as JSON
    numbers. ``storage_size`` is ``u64`` and arrives as a decimal STRING to
    avoid precision loss (the same reason a ``Blob``'s ``blob_id`` does).
    Either form is accepted for the size, so a proto-shape change degrades
    to a loud wrong parse rather than silently reporting every reservation
    as zero bytes.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus
            ``Storage``. The caller is responsible for checking
            ``object_type`` first -- this function reads fields and does
            not verify the object's type.

    Returns:
        StorageObject: The parsed reservation.

    Raises:
        ValueError: If the object carries no JSON struct view, so its
            fields cannot be read at all.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id or '(unknown)'} has no JSON view; "
            "its Storage fields cannot be read."
        )
    return _storage_from_field_map(
        fields=obj.json.struct_value.fields, object_id=obj.object_id or ""
    )


def storage_from_blob(*, obj: sui_prot.Object) -> StorageObject:
    """Read the ``Storage`` EMBEDDED in a fetched ``Blob`` object.

    A ``Blob`` holds its ``Storage`` by value, so the reservation's fields
    are readable straight off the blob's contents even though that wrapped
    object has no independent ID and cannot be fetched on its own. The
    returned :attr:`~pytusk.core.types.receipts.StorageObject.object_id` is
    therefore always ``""``.

    This is what a caller needs before extending a blob with a standalone
    ``Storage``: ``blob::extend_with_resource`` calls ``fuse_periods`` on
    the blob's existing storage, so the extension must satisfy
    :func:`~pytusk.core.ops.storage_compose.fuse_periods_incompatibility`
    against THIS value.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus
            ``Blob``. The caller is responsible for checking
            ``object_type`` first -- this function reads fields and does
            not verify the object's type.

    Returns:
        StorageObject: The blob's embedded reservation, with an empty
            ``object_id``.

    Raises:
        ValueError: If the object carries no JSON view, or has no
            ``storage`` field.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id or '(unknown)'} has no JSON view; "
            "its embedded Storage fields cannot be read."
        )
    storage_val = obj.json.struct_value.fields.get("storage")
    if not (storage_val and storage_val.struct_value):
        raise ValueError(
            f"Object {obj.object_id or '(unknown)'} has no 'storage' "
            "field; it may not be a Walrus Blob."
        )
    return _storage_from_field_map(fields=storage_val.struct_value.fields, object_id="")


def _storage_from_field_map(
    *, fields: dict[str, pb.Value], object_id: str
) -> StorageObject:
    """Build a :class:`~pytusk.core.types.receipts.StorageObject` from a
    Storage struct's field map.

    Shared by :func:`storage_from_object` and :func:`storage_from_blob`,
    which differ only in how they reach these fields.

    ``start_epoch`` and ``end_epoch`` are ``u32`` and arrive as JSON
    numbers. ``storage_size`` is ``u64`` and arrives as a decimal STRING
    to avoid precision loss (the same reason a ``Blob``'s ``blob_id``
    does). Either form is accepted for the size, so a proto-shape change
    degrades to a loud wrong parse rather than silently reporting every
    reservation as zero bytes.

    Args:
        fields (dict[str, pb.Value]): The Storage struct's field map.
        object_id (str): Object ID to record, or ``""`` for a wrapped
            storage that has no independently addressable ID.

    Returns:
        StorageObject: The parsed reservation.
    """
    start_val = fields.get("start_epoch")
    end_val = fields.get("end_epoch")
    size_val = fields.get("storage_size")

    if size_val is None:
        storage_size = 0
    elif size_val.string_value:
        storage_size = int(size_val.string_value)
    else:
        storage_size = int(size_val.number_value or 0)

    return StorageObject(
        object_id=object_id,
        start_epoch=int(start_val.number_value or 0) if start_val else 0,
        end_epoch=int(end_val.number_value or 0) if end_val else 0,
        storage_size=storage_size,
    )
