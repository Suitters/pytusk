#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Storage object command handlers for the tusky CLI.

Handlers for listing, splitting, fusing, reclaiming, and extending blobs
with standalone Walrus Storage objects. Each handler takes the parsed
argparse.Namespace for its subcommand and performs the corresponding
pytusk/pysui operation. Handlers are async; tusky.py drives them via
asyncio.run.
"""

import argparse
import sys

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import GetObject, GetObjectsOwnedByAddress
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction
from pysui.sui.sui_utils import hexstring_to_sui_id

from pytusk import (
    StorageObject,
    WalrusClient,
    add_destroy_storage,
    add_fuse,
    add_split_by_epoch,
    add_split_by_size,
    fuse_incompatibility,
    fuse_periods_incompatibility,
    list_storage_objects,
    storage_from_blob,
    storage_from_object,
)
from pytusk.core.chain import blob_certified_epoch
from pytusk.tusky.tusky_cmds_common import (
    config_from_args,
    resolve_sender,
    resolve_sponsor,
    submit,
    walrus_package_id,
)

_MAX_STORAGE_OPS_PER_PTB = 100


def _split_by_epoch_applicable(*, storage: StorageObject) -> bool:
    """Whether ``storage`` has a valid ``--by-epoch`` split point.

    ``split_by_epoch`` needs an interior epoch (Move asserts
    ``start_epoch < split_epoch < end_epoch``), which only exists when the
    range spans at least two epochs.

    Args:
        storage (StorageObject): The storage object to check.

    Returns:
        bool: True if the epoch range has an interior split point.
    """
    return (storage.end_epoch - storage.start_epoch) >= 2


def _split_by_size_applicable(*, storage: StorageObject) -> bool:
    """Whether ``storage`` has a valid ``--by-size`` split point.

    ``split_by_size`` needs a ``split_size`` that leaves a non-zero
    remainder, which only exists when ``storage_size`` is at least 2
    bytes.

    Args:
        storage (StorageObject): The storage object to check.

    Returns:
        bool: True if the storage size can be peeled into two non-zero
        parts.
    """
    return storage.storage_size >= 2


def _fuse_amount_groups(
    *, storages: list[StorageObject]
) -> list[list[StorageObject]]:
    """Group objects that share an identical epoch range.

    Any two objects with the same ``start_epoch`` AND ``end_epoch`` are
    ALWAYS ``fuse_amount``-compatible regardless of size (size is only
    summed, never gates compatibility) -- and fusing does not change the
    survivor's range, so this relation is TRANSITIVE. A group of N such
    objects can therefore be fully merged into ONE object with a SINGLE
    transaction (N-1 chained ``fuse`` move_calls, in any order).

    Args:
        storages (list[StorageObject]): The candidate objects, already
            filtered by ``--status`` if applicable.

    Returns:
        list[list[StorageObject]]: Every group of 2+ objects sharing an
            identical epoch range, in the order the range was first seen.
            A range held by only one object is omitted (nothing to fuse).
    """
    buckets: dict[tuple[int, int], list[StorageObject]] = {}
    for storage in storages:
        key = (storage.start_epoch, storage.end_epoch)
        buckets.setdefault(key, []).append(storage)
    return [group for group in buckets.values() if len(group) >= 2]


def _fuse_periods_pairs(
    *, storages: list[StorageObject]
) -> list[tuple[StorageObject, StorageObject]]:
    """Every pair compatible ONLY via ``fuse_periods`` (differing start_epoch).

    Unlike :func:`_fuse_amount_groups`, this relation is NOT transitive:
    fusing changes the survivor's range, which can flip which route a
    SUBSEQUENT pairing would take and break it -- e.g. fusing a hub with
    one same-size adjacent spoke shifts the hub's start_epoch to match a
    second spoke's, which then routes to ``fuse_amount`` and fails on a
    mismatched end_epoch. So these are reported as individual pairs only,
    never as an "all N fusable together in one transaction" group.

    Args:
        storages (list[StorageObject]): The candidate objects, already
            filtered by ``--status`` if applicable.

    Returns:
        list[tuple[StorageObject, StorageObject]]: Every compatible pair
            whose members have differing ``start_epoch``.
    """
    pairs: list[tuple[StorageObject, StorageObject]] = []
    for i, first in enumerate(storages):
        for second in storages[i + 1 :]:
            if first.start_epoch == second.start_epoch:
                continue
            if fuse_incompatibility(first=first, second=second) is None:
                pairs.append((first, second))
    return pairs


def _fuse_chain_state(*, first: StorageObject, second: StorageObject) -> StorageObject:
    """Compute the survivor's post-fuse state, mirroring Move locally.

    Used to validate each subsequent fold in a fuse chain against the
    survivor's CURRENT state rather than its original state -- a fold can
    change which route (``fuse_amount``/``fuse_periods``) the next fold
    takes. Callers must have already confirmed the pair is compatible via
    :func:`~pytusk.fuse_incompatibility` or
    :func:`~pytusk.fuse_periods_incompatibility` before calling this.

    Args:
        first (StorageObject): The surviving object BEFORE this fuse.
        second (StorageObject): The object being folded in and consumed.

    Returns:
        StorageObject: The surviving object's state AFTER this fuse --
        same ``object_id`` as ``first``.
    """
    if first.start_epoch == second.start_epoch:
        return StorageObject(
            object_id=first.object_id,
            start_epoch=first.start_epoch,
            end_epoch=first.end_epoch,
            storage_size=first.storage_size + second.storage_size,
        )
    return StorageObject(
        object_id=first.object_id,
        start_epoch=min(first.start_epoch, second.start_epoch),
        end_epoch=max(first.end_epoch, second.end_epoch),
        storage_size=first.storage_size,
    )


def _fuse_periods_chain(
    *, hub: StorageObject, spokes: list[StorageObject]
) -> tuple[list[tuple[str, str]], list[StorageObject]]:
    """Greedily fold every reachable spoke into ``hub`` via ``fuse_periods``.

    Runs to a fixed point: each pass tries every still-unfolded spoke
    against the survivor's CURRENT state (via :func:`_fuse_chain_state`),
    folding in whatever is compatible right now, then loops again --
    folding one spoke can change whether another becomes compatible.
    Stops when a pass folds nothing in. Deliberately restricted to the
    ``fuse_periods`` route only: a spoke that would route to
    ``fuse_amount`` (identical start_epoch as the current survivor) is
    left alone here, since that is the ``--fuse-amount`` mode's job, not
    this one's.

    Args:
        hub (StorageObject): The surviving object spokes fold into.
        spokes (list[StorageObject]): Candidates to fold in, from the same
            ``fuse_periods``-connected cluster as ``hub``.

    Returns:
        tuple[list[tuple[str, str]], list[StorageObject]]: The ordered
        ``(survivor_id, consumed_id)`` move_call pairs to submit, and any
        spokes left over because the fold order broke their compatibility.
    """
    survivor = hub
    remaining = list(spokes)
    pairs: list[tuple[str, str]] = []
    progress = True
    while remaining and progress:
        progress = False
        still_remaining: list[StorageObject] = []
        for spoke in remaining:
            if survivor.start_epoch == spoke.start_epoch:
                still_remaining.append(spoke)
                continue
            if fuse_periods_incompatibility(first=survivor, second=spoke) is None:
                pairs.append((survivor.object_id, spoke.object_id))
                survivor = _fuse_chain_state(first=survivor, second=spoke)
                progress = True
            else:
                still_remaining.append(spoke)
        remaining = still_remaining
    return pairs, remaining


def _periods_components(*, storages: list[StorageObject]) -> list[list[StorageObject]]:
    """Connected components of the ``fuse_periods``-only compatibility graph.

    Builds an undirected graph from :func:`_fuse_periods_pairs` (which
    already excludes same-``start_epoch`` pairs -- those are
    ``fuse_amount``'s territory) and returns each connected component with
    2+ members. Used to resolve ``--fuse-periods`` bulk mode: a wallet can
    hold more than one disjoint cluster, and each needs its own hub.

    Args:
        storages (list[StorageObject]): The candidate objects.

    Returns:
        list[list[StorageObject]]: Each connected component, members
        sorted by ``object_id`` for deterministic ordering.
    """
    pairs = _fuse_periods_pairs(storages=storages)
    by_id = {storage.object_id: storage for storage in storages}
    adjacency: dict[str, set[str]] = {}
    for first, second in pairs:
        adjacency.setdefault(first.object_id, set()).add(second.object_id)
        adjacency.setdefault(second.object_id, set()).add(first.object_id)

    seen: set[str] = set()
    components: list[list[StorageObject]] = []
    for object_id in adjacency:
        if object_id in seen:
            continue
        stack = [object_id]
        component_ids: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component_ids:
                continue
            component_ids.add(current)
            stack.extend(adjacency.get(current, set()) - component_ids)
        seen |= component_ids
        components.append([by_id[i] for i in sorted(component_ids)])
    return components


def _resolve_amount_targets(
    *,
    storages: list[StorageObject],
    start_epoch: int | None,
    end_epoch: int | None,
) -> list[StorageObject]:
    """Resolve which ``fuse_amount`` group ``--fuse-amount`` should fuse.

    Args:
        storages (list[StorageObject]): The candidate objects.
        start_epoch (int | None): ``--start-epoch``, or ``None``.
        end_epoch (int | None): ``--end-epoch``, or ``None``.

    Returns:
        list[StorageObject]: The resolved group, 2+ members.

    Raises:
        ValueError: If no group matches, or the group is ambiguous and
            neither epoch bound was given to disambiguate it.
    """
    groups = _fuse_amount_groups(storages=storages)
    if start_epoch is not None or end_epoch is not None:
        if start_epoch is None or end_epoch is None:
            raise ValueError("--start-epoch and --end-epoch must be given together.")
        group = next(
            (
                g
                for g in groups
                if g[0].start_epoch == start_epoch and g[0].end_epoch == end_epoch
            ),
            None,
        )
        if group is None:
            raise ValueError(
                f"No fuse_amount group exists for epoch range "
                f"[{start_epoch}, {end_epoch})."
            )
        return group
    if not groups:
        raise ValueError("No fuse_amount-compatible objects found.")
    if len(groups) > 1:
        ranges = ", ".join(
            f"[{g[0].start_epoch}, {g[0].end_epoch}) ({len(g)} objects)"
            for g in groups
        )
        raise ValueError(
            "Multiple fuse_amount epoch-range groups exist -- pass "
            "--start-epoch and --end-epoch to name which one: "
            f"{ranges}"
        )
    return groups[0]


def _resolve_periods_targets(
    *, storages: list[StorageObject], fuse_to: str | None
) -> tuple[StorageObject, list[StorageObject]]:
    """Resolve the hub and spokes ``--fuse-periods`` should fuse.

    Args:
        storages (list[StorageObject]): The candidate objects.
        fuse_to (str | None): ``--fuse-to``, naming the hub, or ``None``.

    Returns:
        tuple[StorageObject, list[StorageObject]]: The hub and its spokes.

    Raises:
        ValueError: If ``fuse_to`` names an object outside any cluster, or
            the cluster is ambiguous and ``fuse_to`` was not given to
            disambiguate it.
    """
    components = _periods_components(storages=storages)
    if fuse_to:
        component = next(
            (c for c in components if any(s.object_id == fuse_to for s in c)), None
        )
        if component is None:
            raise ValueError(
                f"{fuse_to} is not part of any fuse_periods-compatible cluster."
            )
        hub = next(s for s in component if s.object_id == fuse_to)
        spokes = [s for s in component if s.object_id != fuse_to]
        return hub, spokes
    if not components:
        raise ValueError("No fuse_periods-compatible objects found.")
    if len(components) > 1:
        clusters = "; ".join(
            "{" + ", ".join(s.object_id for s in c) + "}" for c in components
        )
        raise ValueError(
            "Multiple fuse_periods-compatible clusters exist -- pass "
            "--fuse-to naming any object in the cluster to consolidate "
            f"around: {clusters}"
        )
    component = components[0]
    hub = min(component, key=lambda s: s.object_id)
    spokes = [s for s in component if s.object_id != hub.object_id]
    return hub, spokes


async def _fetch_storage_object(
    *, client: WalrusClient, label: str, object_id: str
) -> StorageObject:
    """Fetch and parse one standalone Storage object by ID, or exit.

    Shared by ``fuse_storage``'s explicit ``--fuse-to``/``--fuse-from``
    mode for every object it needs by ID.

    Args:
        client (WalrusClient): Client used to fetch the object.
        label (str): Human-readable name of the argument being resolved,
            used in error messages (e.g. ``--fuse-to``).
        object_id (str): Object ID to fetch.

    Returns:
        StorageObject: The parsed Storage object.
    """
    fetched = await client.execute(command=GetObject(object_id=object_id))
    if not fetched.is_ok():
        print(
            f"Error fetching {label} object {object_id}: {fetched.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)
    obj = fetched.result_data
    if not (obj.object_type and "::storage_resource::Storage" in obj.object_type):
        print(f"{object_id} ({label}) is not a Walrus Storage object.", file=sys.stderr)
        sys.exit(1)
    try:
        return storage_from_object(obj=obj)
    except ValueError as exc:
        print(f"Error reading {label} {object_id}: {exc}", file=sys.stderr)
        sys.exit(1)


async def _extend_candidate_blobs(
    *, client: WalrusClient, owner: str, current_epoch: int
) -> list[tuple[str, StorageObject]]:
    """Fetch owned Blobs eligible to be extended.

    Eligible means CERTIFIED and not yet expired -- the two
    ``extend_blob_with_storage`` preconditions that depend only on the
    blob itself, independent of which Storage object might extend it.
    Blobs that fail either check, or whose JSON view cannot be parsed,
    are silently skipped -- matching :func:`~pytusk.list_storage_objects`'s
    skip-vs-raise convention for a bulk listing.

    Args:
        client (WalrusClient): Client used to query owned objects.
        owner (str): Address whose Blob objects are examined.
        current_epoch (int): The current Walrus epoch, used to exclude
            already-expired blobs.

    Returns:
        list[tuple[str, StorageObject]]: (blob_object_id, blob's embedded
            storage) for every eligible blob.
    """
    result = await client.execute_for_all(
        command=GetObjectsOwnedByAddress(owner=owner)
    )
    if not result.is_ok():
        return []

    candidates: list[tuple[str, StorageObject]] = []
    for obj in result.result_data.objects:
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            continue
        try:
            certified_epoch = blob_certified_epoch(obj=obj)
        except ValueError:
            continue
        if certified_epoch is None:
            continue
        try:
            blob_storage = storage_from_blob(obj=obj)
        except ValueError:
            continue
        if blob_storage.end_epoch <= current_epoch:
            continue
        candidates.append((obj.object_id, blob_storage))
    return candidates


def _extend_applicable(
    *, storage: StorageObject, candidate_blobs: list[tuple[str, StorageObject]]
) -> bool:
    """Whether ``storage`` could extend at least one candidate blob.

    Mirrors the two ``extend_blob_with_storage`` preconditions that
    depend on the pairing rather than the blob alone: the storage must
    end strictly later than the blob's current end_epoch, and must
    satisfy :func:`~pytusk.fuse_periods_incompatibility` against the
    blob's existing storage -- NOT :func:`~pytusk.fuse_incompatibility`,
    since ``extend_with_resource`` calls ``fuse_periods`` directly.

    Args:
        storage (StorageObject): The standalone storage being checked.
        candidate_blobs (list[tuple[str, StorageObject]]): Blobs already
            filtered to CERTIFIED and not expired, from
            :func:`_extend_candidate_blobs`.

    Returns:
        bool: True if at least one candidate blob is compatible.
    """
    for _blob_id, blob_storage in candidate_blobs:
        if storage.end_epoch <= blob_storage.end_epoch:
            continue
        if fuse_periods_incompatibility(first=blob_storage, second=storage) is None:
            return True
    return False


async def list_storage(args: argparse.Namespace) -> None:
    """List standalone Storage objects owned by the active address.

    Only UNWRAPPED storage appears. A Storage still embedded in a Blob is a
    wrapped object with no independent owner record, so it is invisible to
    an owned-object query -- use `blob`/`blobs` to see those.

    --status compares end_epoch against the current Walrus epoch, the same
    rule `blobs` uses. Expired storage remains splittable, fusable and
    reclaimable, so the filter is a display convenience, not a capability
    gate.

    --details replaces the plain listing with an operation-oriented view:
    a heading per storage-related command (`split_storage`, `fuse_storage`,
    `reclaim_storage`, `extend_blob_with_storage`), each followed by the
    objects it currently applies to. `fuse_storage` lists compatible PAIRS
    rather than individual objects, since fuse always consumes exactly
    two. `extend_blob_with_storage` makes one additional network call to
    fetch owned Blobs, since eligibility depends on them.

    Args:
        args (argparse.Namespace): Parsed `list_storage` subcommand
            arguments, carrying the `status` filter and `details` flag.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        owner = client.pysui_client.config.active_address
        try:
            current_epoch = await client.walrus_epoch()
        except RuntimeError as exc:
            print(f"Cannot get current Walrus epoch: {exc}", file=sys.stderr)
            sys.exit(1)
        _, walrus_pkg = await walrus_package_id(client=client)
        try:
            storages = await list_storage_objects(
                client=client, owner=owner, package_id=walrus_pkg
            )
        except RuntimeError as exc:
            print(f"Error listing Storage objects: {exc}", file=sys.stderr)
            sys.exit(1)

        filtered: list[StorageObject] = []
        for storage in storages:
            status = "expired" if storage.end_epoch <= current_epoch else "active"
            if args.status == "any" or status == args.status:
                filtered.append(storage)

        candidate_blobs: list[tuple[str, StorageObject]] = []
        if args.details and filtered:
            candidate_blobs = await _extend_candidate_blobs(
                client=client, owner=owner, current_epoch=current_epoch
            )

    if not filtered:
        print("No storage objects found matching the given filters.")
        return

    if not args.details:
        for storage in filtered:
            status = "expired" if storage.end_epoch <= current_epoch else "active"
            print(
                f"{storage.object_id}  start_epoch={storage.start_epoch}  "
                f"end_epoch={storage.end_epoch}  size={storage.storage_size}  "
                f"status={status}"
            )
        return

    print("split_storage")
    by_epoch_splittable = [
        s for s in filtered if _split_by_epoch_applicable(storage=s)
    ]
    by_size_splittable = [
        s for s in filtered if _split_by_size_applicable(storage=s)
    ]
    if by_epoch_splittable or by_size_splittable:
        print("  split_by_epoch (epoch range must span >= 2 epochs):")
        if by_epoch_splittable:
            for storage in by_epoch_splittable:
                print(f"    {storage.object_id}")
        else:
            print("    (none)")

        print("  split_by_size (storage size must be >= 2 bytes):")
        if by_size_splittable:
            for storage in by_size_splittable:
                print(f"    {storage.object_id}")
        else:
            print("    (none)")
    else:
        print("  (none)")

    print("fuse_storage")
    amount_groups = _fuse_amount_groups(storages=filtered)
    periods_pairs = _fuse_periods_pairs(storages=filtered)
    if amount_groups or periods_pairs:
        print("  fuse_amount (single transaction fuses all listed together):")
        if amount_groups:
            for group in amount_groups:
                start_epoch, end_epoch = group[0].start_epoch, group[0].end_epoch
                print(f"    epoch range [{start_epoch}, {end_epoch}):")
                for storage in group:
                    print(f"      {storage.object_id}")
        else:
            print("    (none)")

        print(
            "  fuse_periods (pairwise only -- fusing one pair can block "
            "others; not simultaneously mergeable as a set):"
        )
        if periods_pairs:
            for first, second in periods_pairs:
                print(f"    {first.object_id} <-> {second.object_id}")
        else:
            print("    (none)")
    else:
        print("  (none)")

    print("reclaim_storage")
    for storage in filtered:
        print(f"  {storage.object_id}")

    print("extend_blob_with_storage")
    extendable = [
        s
        for s in filtered
        if _extend_applicable(storage=s, candidate_blobs=candidate_blobs)
    ]
    for storage in extendable:
        print(f"  {storage.object_id}")
    if not extendable:
        print("  (none)")


async def split_storage(args: argparse.Namespace) -> None:
    """Split a standalone Storage object by epoch or by size.

    ``split_by_epoch``/``split_by_size`` mutate the original in place and
    RETURN a new Storage. Storage has no ``drop`` ability, so that return
    value must be consumed within the same PTB -- it is transferred to
    --recipient (defaulting to the sender) rather than left dangling,
    which would abort the transaction.

    No epoch gate applies: ``storage_resource`` cannot see the current
    epoch, so an already-expired Storage splits just as happily as a live
    one.

    Args:
        args (argparse.Namespace): Parsed `split_storage` subcommand
            arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        recipient = args.recipient or sender
        _, walrus_pkg = await walrus_package_id(client=client)

        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        if args.by_epoch is not None:
            storage = await add_split_by_epoch(
                txn=txn,
                package_id=walrus_pkg,
                storage_object_id=args.storageid,
                split_epoch=args.by_epoch,
            )
            detail = f"at epoch {args.by_epoch}"
        else:
            storage = await add_split_by_size(
                txn=txn,
                package_id=walrus_pkg,
                storage_object_id=args.storageid,
                split_size=args.by_size,
            )
            detail = f"off {args.by_size} bytes"
        await txn.transfer_objects(transfers=[storage], recipient=recipient)

        txdict = await txn.build_and_sign()
        result = await submit(client=client, txdict=txdict, mode=args.mode)
        if not result.is_ok():
            print(f"Error in split_storage: {result.result_string}", file=sys.stderr)
            sys.exit(1)
        print(f"Split {args.storageid} {detail}; new Storage sent to {recipient}.")
        print(result.result_data.to_json(indent=2))


async def fuse_storage(args: argparse.Namespace) -> None:
    """Fuse Storage objects together, in one of three mutually exclusive modes.

    - Explicit -- ``--fuse-to``/``--fuse-from`` name the surviving object
      and one or more objects to fold into it, in order. Each fold is
      re-validated against the survivor's CURRENT state (not its original
      state) via :func:`_fuse_chain_state`, since a fold can change which
      route -- ``fuse_amount`` or ``fuse_periods`` -- the next one takes.
    - ``--fuse-amount`` -- bulk-fuses every object sharing one epoch-range
      group (see ``list_storage --details``), no object IDs needed. If
      more than one such group is owned, ``--start-epoch``/``--end-epoch``
      name which one (:func:`_resolve_amount_targets`).
    - ``--fuse-periods`` -- bulk-fuses every object reachable, via
      ``fuse_periods``-compatible steps, from one hub -- no object IDs
      needed for the spokes. If more than one disjoint cluster is owned,
      ``--fuse-to`` names the hub to consolidate around
      (:func:`_resolve_periods_targets`, :func:`_fuse_periods_chain`).

    ``--fuse-periods`` chaining is greedy and can leave objects unfused if
    an earlier fold changes the survivor's range enough to break a later
    one -- Move's own non-transitivity (see the module's fuse_amount vs
    fuse_periods design notes), not a bug here. Anything left over is
    reported, not silently dropped.

    Args:
        args (argparse.Namespace): Parsed `fuse_storage` subcommand
            arguments.
    """
    if args.fuse_amount and args.fuse_from:
        print("Error: --fuse-from is not used with --fuse-amount.", file=sys.stderr)
        sys.exit(1)
    if args.fuse_periods and args.fuse_from:
        print("Error: --fuse-from is not used with --fuse-periods.", file=sys.stderr)
        sys.exit(1)
    if args.fuse_amount and args.fuse_to:
        print("Error: --fuse-to is not used with --fuse-amount.", file=sys.stderr)
        sys.exit(1)
    if (
        not args.fuse_amount
        and not args.fuse_periods
        and (not args.fuse_to or not args.fuse_from)
    ):
        print(
            "Error: --fuse-to and --fuse-from are required unless "
            "--fuse-amount or --fuse-periods is set.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        _, walrus_pkg = await walrus_package_id(client=client)

        skipped: list[StorageObject] = []
        if args.fuse_amount:
            try:
                storages = await list_storage_objects(
                    client=client, owner=sender, package_id=walrus_pkg
                )
                group = _resolve_amount_targets(
                    storages=storages,
                    start_epoch=args.start_epoch,
                    end_epoch=args.end_epoch,
                )
            except (RuntimeError, ValueError) as exc:
                print(f"Error in fuse_storage: {exc}", file=sys.stderr)
                sys.exit(1)
            survivor_id = group[0].object_id
            pairs = [(survivor_id, storage.object_id) for storage in group[1:]]
        elif args.fuse_periods:
            try:
                storages = await list_storage_objects(
                    client=client, owner=sender, package_id=walrus_pkg
                )
                hub, spokes = _resolve_periods_targets(
                    storages=storages, fuse_to=args.fuse_to
                )
            except (RuntimeError, ValueError) as exc:
                print(f"Error in fuse_storage: {exc}", file=sys.stderr)
                sys.exit(1)
            survivor_id = hub.object_id
            pairs, skipped = _fuse_periods_chain(hub=hub, spokes=spokes)
            if not pairs:
                print(
                    "No fuse_periods-compatible spokes could be folded in.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            survivor_id = args.fuse_to
            survivor = await _fetch_storage_object(
                client=client, label="--fuse-to", object_id=args.fuse_to
            )
            pairs = []
            for object_id in args.fuse_from:
                spoke = await _fetch_storage_object(
                    client=client, label="--fuse-from", object_id=object_id
                )
                reason = fuse_incompatibility(first=survivor, second=spoke)
                if reason is not None:
                    print(f"Cannot fuse: {reason}", file=sys.stderr)
                    sys.exit(1)
                pairs.append((survivor.object_id, spoke.object_id))
                survivor = _fuse_chain_state(first=survivor, second=spoke)

        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        for first_id, second_id in pairs:
            await add_fuse(
                txn=txn,
                package_id=walrus_pkg,
                first_storage_id=first_id,
                second_storage_id=second_id,
            )
        txdict = await txn.build_and_sign()
        result = await submit(client=client, txdict=txdict, mode=args.mode)
        if not result.is_ok():
            print(f"Error in fuse_storage: {result.result_string}", file=sys.stderr)
            sys.exit(1)
        print(f"Fused {len(pairs)} object(s) into {survivor_id}.")
        if skipped:
            print(
                "Could not fold in (fold order changed compatibility): "
                + ", ".join(storage.object_id for storage in skipped),
                file=sys.stderr,
            )
        print(result.result_data.to_json(indent=2))


async def _destroy_storage_batches(
    *,
    client: WalrusClient,
    walrus_pkg: str,
    storage_ids: list[str],
    sender: str,
    sponsor: str | None,
    mode: str,
    label: str = "Destroyed batch",
) -> list[sui_prot.ExecutedTransaction | sui_prot.SimulateTransactionResponse]:
    """Destroy Storage objects via storage_resource::destroy, batched
    _MAX_STORAGE_OPS_PER_PTB per PTB.

    Mirrors :func:`_burn_blob_batches`'s batching pattern exactly (same
    reasons: keep move_call count per PTB under a safe gas/size ceiling,
    and never buffer results until the end). Each batch's result is
    printed to stdout as soon as it completes, so a failure partway
    through still leaves a full record of every batch that succeeded
    beforehand. A single invalid or already-consumed Storage ID within a
    batch aborts that entire batch (all-or-nothing per batch); other,
    already-submitted batches are unaffected. Unlike ``fuse_storage``,
    ``destroy`` has no preconditions and no cross-object dependency, so
    there is no compatibility or ordering concern here -- only the
    gas/PTB-size ceiling matters.

    In ``execute`` mode, each batch's response carries the per-object Sui
    storage rebate for every destroyed Storage object
    (``result.result_data.objects.objects``, a list sorted by
    ``(object_id, version)`` -- not a map, so a mutated object can appear
    more than once), and one MIST value is printed per destroyed object
    at no extra network cost. The running TOTAL, however, is sourced
    from ``effects.gas_used.storage_rebate`` -- the aggregate figure the
    network actually credits for the whole transaction -- rather than
    summed from the per-object values, since each object's
    ``storage_rebate`` is its own theoretical rebate and is not
    guaranteed to equal an even split of what gets credited. ``simulate``
    mode's response is a ``SimulateTransactionResponse``, not an
    ``ExecutedTransaction`` -- the rebate summary is skipped entirely in
    that mode not because the data is unreachable (it exists one level
    deeper, at ``result.result_data.transaction.objects``), but because
    a dry run's figures are not what execution would actually credit.

    Args:
        client (WalrusClient): Client used to build and submit transactions.
        walrus_pkg (str): Walrus package ID (from System.package_id).
        storage_ids (list[str]): Sui object IDs of Storage objects to
            destroy.
        sender (str): Resolved sender address.
        sponsor (str | None): Resolved sponsor address, if any.
        mode (str): "simulate" or "execute".
        label (str): Prefix used in each batch's progress line.

    Returns:
        list[sui_prot.ExecutedTransaction | sui_prot.SimulateTransactionResponse]:
            One result per successfully submitted batch transaction.
    """
    results: list[
        sui_prot.ExecutedTransaction | sui_prot.SimulateTransactionResponse
    ] = []
    total_rebate = 0
    rebate_recorded = False
    total_batches = (
        len(storage_ids) + _MAX_STORAGE_OPS_PER_PTB - 1
    ) // _MAX_STORAGE_OPS_PER_PTB
    for batch_num, batch_start in enumerate(
        range(0, len(storage_ids), _MAX_STORAGE_OPS_PER_PTB), start=1
    ):
        batch = storage_ids[batch_start : batch_start + _MAX_STORAGE_OPS_PER_PTB]
        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        for storage_id in batch:
            await add_destroy_storage(
                txn=txn,
                package_id=walrus_pkg,
                storage_object_id=storage_id,
            )
        txdict = await txn.build_and_sign()
        result = await submit(client=client, txdict=txdict, mode=mode)
        if not result.is_ok():
            print(
                f"Error destroying batch {batch_num}/{total_batches} "
                f"({len(batch)} storage object(s)): {result.result_string}",
                file=sys.stderr,
            )
            if results:
                print(
                    f"{len(results)}/{total_batches} batch(es) destroyed "
                    "successfully before this failure (see above for their "
                    "receipts).",
                    file=sys.stderr,
                )
            sys.exit(1)
        print(f"{label} {batch_num}/{total_batches} ({len(batch)} storage object(s)).")
        print(result.result_data.to_json(indent=2))
        if isinstance(result.result_data, sui_prot.ExecutedTransaction):
            batch_ids = {
                hexstring_to_sui_id(storage_id).lower() for storage_id in batch
            }
            if result.result_data.objects is not None:
                for obj in result.result_data.objects.objects:
                    if obj.object_id is None or obj.storage_rebate is None:
                        continue
                    if hexstring_to_sui_id(obj.object_id).lower() in batch_ids:
                        print(
                            f"  {obj.object_id}: {obj.storage_rebate} MIST "
                            "storage rebate value"
                        )
            effects = result.result_data.effects
            if effects is not None and effects.gas_used is not None:
                batch_rebate = effects.gas_used.storage_rebate
                if batch_rebate is not None:
                    total_rebate += batch_rebate
                    rebate_recorded = True
        results.append(result.result_data)
    if rebate_recorded:
        print(f"Total storage rebate redeemed: {total_rebate} MIST.")
    return results


async def reclaim_storage(args: argparse.Namespace) -> None:
    """Destroy one or more standalone Storage objects.

    ``storage_resource::destroy`` consumes each object and returns its
    Sui storage rebate to the sender. It does NOT refund the WAL
    originally paid to reserve the capacity -- that is spent regardless.
    There is no epoch gate either, so an unexpired Storage can be
    destroyed, throwing away capacity that was paid for. Irreversible.

    ``-i``/``--storageid`` accepts one or more explicit object IDs;
    ``--all`` destroys every currently owned unwrapped Storage object
    instead, with no object IDs needed. Either way, destroys are batched
    :data:`_MAX_STORAGE_OPS_PER_PTB` per PTB via
    :func:`_destroy_storage_batches`.

    Args:
        args (argparse.Namespace): Parsed `reclaim_storage` subcommand
            arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        _, walrus_pkg = await walrus_package_id(client=client)

        if args.all:
            try:
                storages = await list_storage_objects(
                    client=client, owner=sender, package_id=walrus_pkg
                )
            except RuntimeError as exc:
                print(f"Error listing Storage objects: {exc}", file=sys.stderr)
                sys.exit(1)
            storage_ids = [storage.object_id for storage in storages]
            if not storage_ids:
                print("No storage objects found to destroy.")
                return
        else:
            storage_ids = args.storageid

        await _destroy_storage_batches(
            client=client,
            walrus_pkg=walrus_pkg,
            storage_ids=storage_ids,
            sender=sender,
            sponsor=sponsor,
            mode=args.mode,
            label="Destroyed batch",
        )


async def extend_blob_with_storage(args: argparse.Namespace) -> None:
    """Extend a blob's expiration by consuming an owned Storage object.

    ``system::extend_blob_with_resource`` pays for the extension with a
    Storage object instead of WAL. It bottoms out in
    ``blob::extend_with_resource``, which imposes four requirements -- all
    pre-flighted here so a violation reports the offending values instead
    of surfacing as an opaque Move abort:

    - the blob must be CERTIFIED (ENotCertified);
    - the blob must not already be expired (EResourceBounds);
    - the extension must end strictly LATER than the blob's current
      end_epoch (EResourceBounds);
    - the extension must satisfy ``fuse_periods`` against the blob's
      existing storage -- equal size (EIncompatibleAmount) and adjacency
      (EIncompatibleEpochs).

    That last check uses :func:`~pytusk.fuse_periods_incompatibility`, NOT
    :func:`~pytusk.fuse_incompatibility`: ``extend_with_resource`` calls
    ``fuse_periods`` outright rather than going through ``fuse``'s
    start_epoch dispatch, so the two disagree on a pair that happens to
    share a start_epoch.

    The Storage is consumed by the call and ceases to exist.

    Args:
        args (argparse.Namespace): Parsed `extend_blob_with_storage`
            subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        blob_result = await client.execute(command=GetObject(object_id=args.blobid))
        if not blob_result.is_ok():
            print(
                f"Error fetching blob object: {blob_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        blob_obj = blob_result.result_data
        if not (blob_obj.object_type and "::blob::Blob" in blob_obj.object_type):
            print(f"{args.blobid} is not a Walrus Blob object.", file=sys.stderr)
            sys.exit(1)
        try:
            blob_storage = storage_from_blob(obj=blob_obj)
        except ValueError as exc:
            print(f"Error reading blob {args.blobid}: {exc}", file=sys.stderr)
            sys.exit(1)
        blob_end_epoch = blob_storage.end_epoch

        try:
            certified_epoch = blob_certified_epoch(obj=blob_obj)
        except ValueError as exc:
            print(f"Error reading blob {args.blobid}: {exc}", file=sys.stderr)
            sys.exit(1)
        if certified_epoch is None:
            print(
                f"{args.blobid} is not certified; only certified blobs can "
                "be extended (Move abort: ENotCertified).",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            current_epoch = await client.walrus_epoch()
        except RuntimeError as exc:
            print(f"Cannot get current Walrus epoch: {exc}", file=sys.stderr)
            sys.exit(1)
        if blob_end_epoch <= current_epoch:
            print(
                f"{args.blobid} is expired (end_epoch={blob_end_epoch}, "
                f"current_epoch={current_epoch}); expired blobs cannot be "
                "extended.",
                file=sys.stderr,
            )
            sys.exit(1)

        storage_result = await client.execute(
            command=GetObject(object_id=args.storageid)
        )
        if not storage_result.is_ok():
            print(
                f"Error fetching Storage object: {storage_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        storage_obj = storage_result.result_data
        if not (
            storage_obj.object_type
            and "::storage_resource::Storage" in storage_obj.object_type
        ):
            print(
                f"{args.storageid} is not a Walrus Storage object.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            extension = storage_from_object(obj=storage_obj)
        except ValueError as exc:
            print(f"Error reading Storage {args.storageid}: {exc}", file=sys.stderr)
            sys.exit(1)

        if extension.end_epoch <= blob_end_epoch:
            print(
                f"Storage {args.storageid} ends at epoch "
                f"{extension.end_epoch}, which is not later than blob "
                f"{args.blobid}'s current end_epoch {blob_end_epoch}; the "
                "extension would not extend anything (Move abort: "
                "EResourceBounds).",
                file=sys.stderr,
            )
            sys.exit(1)

        reason = fuse_periods_incompatibility(
            first=blob_storage, second=extension
        )
        if reason is not None:
            print(f"Cannot extend: {reason}", file=sys.stderr)
            sys.exit(1)

        system_obj_id, walrus_pkg = await walrus_package_id(client=client)

        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        await txn.move_call(
            target=f"{walrus_pkg}::system::extend_blob_with_resource",
            arguments=[system_obj_id, args.blobid, args.storageid],
            type_arguments=[],
        )
        txdict = await txn.build_and_sign()
        result = await submit(client=client, txdict=txdict, mode=args.mode)
        if not result.is_ok():
            print(
                f"Error in extend_blob_with_storage: {result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"Extended {args.blobid} from epoch {blob_end_epoch} to "
            f"{extension.end_epoch} using Storage {args.storageid}."
        )
        print(result.result_data.to_json(indent=2))
