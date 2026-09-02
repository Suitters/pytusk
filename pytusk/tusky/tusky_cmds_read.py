#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Read command handlers for the tusky CLI.

Handlers for reading blob/quilt content via the Walrus HTTP aggregator.
Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk operation. Handlers are async; tusky.py
drives them via asyncio.run.
"""

import argparse
import json
import sys

from pysui import GetObject

from pytusk import (
    BlobData,
    DeletableStatus,
    InvalidStatus,
    ListQuiltPatches,
    NonexistentStatus,
    PermanentStatus,
    QuiltPatch,
    QuiltPatchListing,
    ReadBlob,
    ReadQuiltPatch,
    WalrusClient,
    blob_deletable_and_end_epoch,
    blob_id_from_object,
    blob_id_from_url_base64,
    blob_id_to_url_base64,
    fetch_blob_status,
    resolve_blob_sui_objects,
)
from pytusk.tusky.tusky_cmds_common import config_from_args


async def read_blob(args: argparse.Namespace) -> None:
    """Read a blob via the Walrus HTTP aggregator and write its content to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_blob` subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=ReadBlob(blob_id=args.blob_id))
    if not result.is_ok():
        print(f"Error reading blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: BlobData = result.result_data
    sys.stdout.buffer.write(data.content)
    if sys.stdout.isatty():
        sys.stdout.buffer.write(b"\n")


async def read_quilt(args: argparse.Namespace) -> None:
    """Read a single quilt patch via the Walrus HTTP aggregator and write it to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_quilt` subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(
            command=ReadQuiltPatch(quilt_id=args.quilt_id, patch_key=args.patch_key)
        )
    if not result.is_ok():
        print(f"Error reading quilt patch: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: QuiltPatch = result.result_data
    sys.stdout.buffer.write(data.content)
    if sys.stdout.isatty():
        sys.stdout.buffer.write(b"\n")


async def quilt_patches(args: argparse.Namespace) -> None:
    """List the patches contained in a quilt via the Walrus HTTP aggregator.

    Gates on expiry first: a quilt is a blob like any other, so its content
    can be expired or never-existed, both of which the aggregator's
    list-patches-in-quilt endpoint reports identically as a bare
    BLOB_NOT_FOUND. Resolving the committee's own verdict first gives a
    clearer answer, and for a still-live blob avoids ever reaching the
    aggregator ambiguity at all.

    Args:
        args (argparse.Namespace): Parsed `quilt_patches` subcommand
            arguments. Exactly one of `blob_id`/`object_id` is set, enforced
            by a required mutually-exclusive group.
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

        tier: int | None = None
        end_epoch: int | None = None

        if isinstance(report.status, (NonexistentStatus, InvalidStatus)):
            print(
                f"Quilt does not exist: committee status is "
                f"{type(report.status).__name__}.",
                file=sys.stderr,
            )
            sys.exit(1)
        if isinstance(report.status, PermanentStatus):
            end_epoch = report.status.end_epoch
            if end_epoch <= report.committee_epoch:
                print(
                    f"Quilt is expired: end_epoch={end_epoch} "
                    f"committee_epoch={report.committee_epoch}",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif isinstance(report.status, DeletableStatus):
            # Committee status carries no end_epoch for a deletable
            # registration -- only a resolved on-chain object can answer
            # this. An empty or undetermined resolution is NOT evidence of
            # expiry and must not block the read; only an object we
            # actually resolved AND determined expired blocks it, and only
            # if every determinable object agrees. Not tracking `break` on
            # the first unexpired hit any more -- every object is walked so
            # `end_epoch` can report the LATEST covering registration, not
            # just whichever was resolved first.
            resolved_objects, tier = await resolve_blob_sui_objects(
                client=client,
                blob_id=blob_id,
                owner=owner,
                report=report,
                known_blob_sui_object=known_blob_sui_object,
            )
            determined_any = False
            any_unexpired = False
            for obj in resolved_objects:
                try:
                    _, obj_end_epoch = blob_deletable_and_end_epoch(obj=obj)
                except ValueError:
                    continue
                determined_any = True
                if end_epoch is None or obj_end_epoch > end_epoch:
                    end_epoch = obj_end_epoch
                if obj_end_epoch > report.committee_epoch:
                    any_unexpired = True
            if determined_any and not any_unexpired:
                print(
                    "Quilt is expired: every resolved blob_sui_object's "
                    "storage period has ended.",
                    file=sys.stderr,
                )
                sys.exit(1)
        # UnresolvedStatus: no verdict reached, so there is no reliable
        # expiry signal either way -- proceed and let the aggregator answer.

        quilt_id = blob_id_to_url_base64(blob_id=blob_id)
        result = await client.execute(command=ListQuiltPatches(quilt_id=quilt_id))
    if not result.is_ok():
        print(f"Error listing quilt patches: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    listing: QuiltPatchListing = result.result_data
    enriched: dict[str, object] = {
        "blob_id": quilt_id,
        "status": type(report.status).__name__,
        "committee_epoch": report.committee_epoch,
    }
    if end_epoch is not None:
        enriched["end_epoch"] = end_epoch
    if tier is not None:
        enriched["ladder_tier"] = tier
    enriched["patches"] = listing.to_dict()["patches"]
    print(json.dumps(enriched, indent=2))
