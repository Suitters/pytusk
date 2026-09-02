#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Query command handlers for the tusky CLI.

Read-only informational commands: listing/inspecting owned blobs, the
current Walrus epoch, the active storage committee, and owned WAL coins.
Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk/pysui operation. Handlers are async;
tusky.py drives them via asyncio.run.
"""

import argparse
import sys

from pysui import GetCoins, GetObject, GetObjectsOwnedByAddress

from pytusk import (
    DeletableStatus,
    DissentReason,
    InvalidStatus,
    NonexistentStatus,
    PermanentStatus,
    Resolution,
    UnresolvedStatus,
    WalrusClient,
    blob_certified_epoch,
    blob_deletable_and_end_epoch,
    blob_id_from_object,
    blob_id_from_url_base64,
    blob_id_to_url_base64,
    fetch_blob_status,
    resolve_blob_sui_objects,
    storage_from_blob,
)
from pytusk.tusky.tusky_cmds_common import (
    config_from_args,
    wal_balance_and_decimals,
)


async def blobs(args: argparse.Namespace) -> None:
    """List blobs owned by the active address, with optional filtering.

    Args:
        args (argparse.Namespace): Parsed `blobs` subcommand arguments,
            including `deletable` ("any"/"true"/"false") and `status`
            ("any"/"active"/"expired") filters.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        current_epoch = await client.walrus_epoch()
        owner = client.pysui_client.config.active_address
        objects_result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
    if not objects_result.is_ok():
        print(
            f"Error listing owned objects: {objects_result.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)

    found = False
    for obj in objects_result.result_data.objects:
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            continue

        # blobs() lists everything a user owns, so one object with an
        # incomplete/malformed JSON view (e.g. a partial RPC response)
        # must degrade its own row rather than abort the whole listing --
        # unlike expiry_report(), which is strict by design. Each field
        # extraction below is guarded independently and falls back to the
        # same defaults the pre-refactor hand-walked code used.
        try:
            deletable, end_epoch = blob_deletable_and_end_epoch(obj=obj)
        except ValueError:
            deletable, end_epoch = False, 0
        status = "expired" if end_epoch <= current_epoch else "active"

        if args.deletable != "any" and str(deletable).lower() != args.deletable:
            continue
        if args.status != "any" and status != args.status:
            continue

        try:
            blob_id_b64 = blob_id_to_url_base64(blob_id=blob_id_from_object(obj=obj))
        except (ValueError, OverflowError):
            blob_id_b64 = "(unparseable)"

        found = True
        try:
            blob_size = storage_from_blob(obj=obj).storage_size
        except ValueError:
            # Same missing-JSON-view/missing-'storage'-field cases that
            # blob_deletable_and_end_epoch() already tolerated above would
            # otherwise raise here too and still abort the listing.
            blob_size = 0
        print(
            f"{obj.object_id}  blob_id={blob_id_b64}  "
            f"deletable={deletable}  end_epoch={end_epoch}  status={status}  "
            f"size={blob_size}"
        )

    if not found:
        print("No blobs found matching the given filters.")


async def expiry_report(args: argparse.Namespace) -> None:
    """Print an aging report of owned blobs, sorted soonest-to-expire first.

    Lists each blob's Sui object ID alongside its storage end_epoch, the
    current Walrus epoch, and the number of epochs remaining before
    expiration. A blob's status is "uncertified" if it has not yet been
    certified by storage nodes (regardless of remaining epochs), otherwise
    "expired" when remaining epochs is negative, "expiring" when exactly
    zero, and "active" when positive.

    Args:
        args (argparse.Namespace): Parsed `expiry_report` subcommand
            arguments, including an optional `address` override.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        current_epoch = await client.walrus_epoch()
        owner = args.address or client.pysui_client.config.active_address
        objects_result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
    if not objects_result.is_ok():
        print(
            f"Error listing owned objects: {objects_result.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)

    rows: list[tuple[str, int, int, bool]] = []
    for obj in objects_result.result_data.objects:
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            continue
        _, end_epoch = blob_deletable_and_end_epoch(obj=obj)
        certified = blob_certified_epoch(obj=obj) is not None
        rows.append((obj.object_id, end_epoch, end_epoch - current_epoch, certified))

    if not rows:
        print("No blobs found.")
        return

    rows.sort(key=lambda row: row[2])

    print(
        f"{'OBJECT ID':<66}  {'END_EPOCH':>10}  {'CURRENT_EPOCH':>13}  "
        f"{'REMAINING':>9}  STATUS"
    )
    for object_id, end_epoch, remaining, certified in rows:
        if not certified:
            status = "uncertified"
        elif remaining < 0:
            status = "expired"
        elif remaining == 0:
            status = "expiring"
        else:
            status = "active"
        print(
            f"{object_id:<66}  {end_epoch:>10}  {current_epoch:>13}  "
            f"{remaining:>9}  {status}"
        )


async def blob(args: argparse.Namespace) -> None:
    """Show full on-chain details for one blob object.

    Args:
        args (argparse.Namespace): Parsed `blob` subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=GetObject(object_id=args.object_id))
    if not result.is_ok():
        print(f"Error fetching object: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


# Verdict variant -> the word printed for it. A mapping rather than a
# derived name so the CLI vocabulary is chosen here, not inherited from
# Python class names that exist for other reasons.
_STATUS_WORD = {
    PermanentStatus: "permanent",
    DeletableStatus: "deletable",
    NonexistentStatus: "nonexistent",
    InvalidStatus: "invalid",
    UnresolvedStatus: "unresolved",
}

# Exit code for "the committee could not tell us", kept DISTINCT from the
# 1 used for ordinary CLI failures: an automation caller must be able to
# separate "your invocation was wrong" from "the network did not answer".
_EXIT_UNRESOLVED = 2


def _epochs_remaining(count: int) -> str:
    """Render an epoch count with singular/plural agreement.

    Args:
        count (int): Number of epochs remaining.

    Returns:
        str: e.g. "1 epoch remaining" or "2 epochs remaining".
    """
    unit = "epoch" if count == 1 else "epochs"
    return f"{count} {unit} remaining"


async def blob_status(args: argparse.Namespace) -> None:
    """Report the storage committee's verdict on one blob, with blob_sui_object facts.

    Answers for the CONTENT, not for an object: the verdict comes from
    fanning a status read across the whole committee and resolving the
    answers against shard-weight thresholds. On-chain blob_sui_object
    details are then layered on where they can be reached, via the
    four-tier ladder described in the ``-b`` help text. A thin result
    means no object was reachable, NOT that the blob has none.

    Args:
        args (argparse.Namespace): Parsed `blob_status` subcommand
            arguments. Exactly one of ``blob_id`` / ``object_id`` is set,
            enforced by a required mutually-exclusive group.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        staking_object = client.config.network.staking_object
        owner = client.pysui_client.config.active_address

        known_blob_sui_object = None
        if args.object_id is not None:
            named = await client.execute(command=GetObject(object_id=args.object_id))
            if not named.is_ok():
                print(
                    f"Error fetching object: {named.result_string}", file=sys.stderr
                )
                sys.exit(1)
            # A well-formed object ID that names nothing comes back OK with
            # an empty Object (no fields set) -- distinct from an object that
            # exists but is not a Blob. Check for empty object_id to distinguish
            # the case and report a clearer message than "not a Walrus Blob object",
            # which sends the operator looking for a type error when the ID is
            # simply wrong or the object was deleted/pruned.
            if named.result_data is None or not named.result_data.object_id:
                print(
                    f"No object found for {args.object_id}. It may never have "
                    "existed, or has been deleted or pruned.",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                blob_id = blob_id_from_object(obj=named.result_data)
            except ValueError as exc:
                print(f"Not a Walrus Blob object: {exc}", file=sys.stderr)
                sys.exit(1)
            # -o always lands at tier-2 richness: the object is in hand.
            known_blob_sui_object = named.result_data
        else:
            blob_id = blob_id_from_url_base64(value=args.blob_id)

        try:
            report = await fetch_blob_status(
                client=client,
                blob_id=blob_id,
                staking_object=staking_object,
                timeout_seconds=args.timeout,
            )
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Cannot query blob status: {exc}", file=sys.stderr)
            sys.exit(1)

        blob_sui_objects, tier = await resolve_blob_sui_objects(
            client=client,
            blob_id=blob_id,
            owner=owner,
            report=report,
            known_blob_sui_object=known_blob_sui_object,
        )

    word = _STATUS_WORD.get(type(report.status), "unknown")
    print(f"blob_id: {blob_id_to_url_base64(blob_id=report.blob_id)}")
    print(f"status: {word}")
    # Three buckets, not one. A node that never answered has NOT dissented:
    # collapsing "unreachable" into "dissenting" reads as a committee split
    # when the committee is actually near-unanimous and merely patchy in
    # reachability -- on a real testnet query that was 2 genuine dissents
    # reported as 28. Same principle as E4: a failure to determine must
    # never render identically to a determination.
    dissented = sum(
        1
        for d in report.dissenting
        if d.reason in (DissentReason.DISAGREED, DissentReason.NOT_STORED)
    )
    unreachable = sum(
        1
        for d in report.dissenting
        if d.reason in (DissentReason.ERROR, DissentReason.TIMEOUT)
    )
    not_checked = sum(
        1 for d in report.dissenting if d.reason is DissentReason.NOT_CHECKED
    )
    print(
        f"resolution: {report.resolution.value.upper()} "
        f"({len(report.confirming)} confirming, {dissented} dissenting, "
        f"{unreachable} unreachable, {not_checked} not-checked)"
    )
    print(f"committee_epoch: {report.committee_epoch}")
    print(f"ladder_tier: {tier}")

    if isinstance(report.status, PermanentStatus):
        remaining = report.status.end_epoch - report.committee_epoch
        print(
            f"end_epoch: {report.status.end_epoch} "
            f"({_epochs_remaining(remaining)})"
        )
        print(f"certified: {report.status.is_certified}")
        if report.status.initial_certified_epoch is not None:
            print(f"initial_certified_epoch: {report.status.initial_certified_epoch}")
    if isinstance(report.status, (PermanentStatus, DeletableStatus)):
        counts = report.status.deletable_counts
        print(f"deletable_objects: {counts.total} total, {counts.certified} certified")

    for obj in blob_sui_objects:
        try:
            deletable, end_epoch = blob_deletable_and_end_epoch(obj=obj)
        except ValueError:
            deletable, end_epoch = False, 0
        remaining = end_epoch - report.committee_epoch
        print(
            f"blob_sui_object: {obj.object_id}  deletable={deletable}  "
            f"end_epoch={end_epoch} ({_epochs_remaining(remaining)})"
        )

    if args.details:
        print("confirming:")
        for node in report.confirming:
            print(f"  {node.node_id}  {node.weight:>4} shards  {node.network_address}")
        print("dissenting:")
        for dissent in report.dissenting:
            print(
                f"  {dissent.node.node_id}  {dissent.node.weight:>4} shards  "
                f"{dissent.reason.value}"
            )

    if report.resolution is Resolution.UNRESOLVED:
        total = len(report.confirming) + len(report.dissenting)
        print(
            f"No verdict: {unreachable + not_checked} of {total} committee "
            f"members supplied no status before the deadline; {dissented} "
            "answered but no status reached a threshold.",
            file=sys.stderr,
        )
        sys.exit(_EXIT_UNRESOLVED)


async def epoch(args: argparse.Namespace) -> None:
    """Print the current Walrus epoch.

    Args:
        args (argparse.Namespace): Parsed `epoch` subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            current_epoch = await client.walrus_epoch()
        except RuntimeError as exc:
            print(f"Cannot get current Walrus epoch: {exc}", file=sys.stderr)
            sys.exit(1)
    print(current_epoch)


async def committee(args: argparse.Namespace) -> None:
    """Print the active Walrus storage committee.

    One line is printed per member. A member's leading index is its committee
    position, which is what ``signers_bitmap`` indexes. Public keys are shown
    truncated; they are 96 bytes on chain.

    Args:
        args (argparse.Namespace): Parsed `committee` subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            walrus_committee = await client.committee()
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Cannot get Walrus committee: {exc}", file=sys.stderr)
            sys.exit(1)
    print(
        f"epoch {walrus_committee.epoch}  "
        f"shards {walrus_committee.n_shards}  "
        f"members {walrus_committee.committee_size}"
    )
    for position, member in enumerate(walrus_committee.members):
        public_key = member.public_key.hex()
        print(
            f"{position:>4}  "
            f"{len(member.shard_indices):>4} shards  "
            f"{member.node_id}  "
            f"{public_key[:8]}..{public_key[-8:]}  "
            f"{member.network_address}"
        )


async def wal_coins(args: argparse.Namespace) -> None:
    """List WAL coin objects owned by an address, styled on pysui's gas layout.

    Args:
        args (argparse.Namespace): Parsed `wal_coins` subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        owner = args.address or client.pysui_client.config.active_address
        wal_entry, decimals = await wal_balance_and_decimals(
            client=client, owner=owner
        )
        coins_result = await client.execute_for_all(
            command=GetCoins(
                owner=owner, coin_type=f"0x2::coin::Coin<{wal_entry.coin_type}>"
            )
        )
    if not coins_result.is_ok():
        print(f"Error listing WAL coins: {coins_result.result_string}", file=sys.stderr)
        sys.exit(1)

    divisor = 10**decimals
    print()
    print(f"{'WAL Object ID':^75}   {'Frost':>12}   {'WAL':>10}")
    print("-" * 107)
    for coin in coins_result.result_data.objects:
        balance = coin.balance or 0
        print(f"{coin.object_id} has {balance:>12} -> {balance / divisor:>8.4f}")
    print()

    coin_balance = wal_entry.coin_balance or 0
    address_balance = wal_entry.address_balance or 0
    total = wal_entry.balance or 0
    print(f"Coin-All wal: {coin_balance:>12} -> {coin_balance / divisor:>8.4f}")
    print(f"Addr-All wal: {address_balance:>12} -> {address_balance / divisor:>8.4f}")
    print(f"   Total wal: {total:>12} -> {total / divisor:>8.4f}")
