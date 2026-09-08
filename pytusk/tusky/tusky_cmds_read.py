#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Read command handlers for the tusky CLI.

Handlers for reading blob/quilt content, either via the Walrus HTTP
aggregator or -- for ``read_blob_native`` -- by fetching slivers from the
storage nodes and reconstructing the blob locally, with no aggregator in
the path. Each handler takes the parsed argparse.Namespace for its
subcommand and performs the corresponding pytusk operation. Handlers are
async; tusky.py drives them via asyncio.run.
"""

import argparse
import asyncio
import json
import sys

from pysui import GetObject

from pytusk import (
    BlobData,
    BlobDecodeError,
    DeletableStatus,
    InvalidStatus,
    NativeReadError,
    NonexistentStatus,
    PermanentStatus,
    QuiltPatch,
    QuiltPatchListing,
    ReadBlob,
    ReadQuiltPatch,
    ReadQuiltPatchById,
    ReadQuiltPatches,
    WalrusClient,
    blob_deletable_and_end_epoch,
    blob_id_from_object,
    blob_id_from_url_base64,
    blob_id_to_url_base64,
    fetch_blob_status,
    resolve_blob_sui_objects,
)
from pytusk import read_blob_native as _read_blob_native_pipeline
from pytusk.tusky.tusky_cmds_common import config_from_args, write_file_bytes


def _refuse_tty_stdout() -> None:
    """Exit with an error if stdout is a terminal, before any network I/O.

    Blob and quilt content is untrusted, arbitrary binary data -- unlike
    the HTTP error-body text filtered in
    :func:`pytusk.commands.walrus_command._response_body_for_log`, there is
    no way to strip terminal escape sequences from arbitrary bytes without
    risking corruption of content that isn't text at all (an image, an
    archive, ...). Checked before the aggregator fetch or committee
    read/reconstruct runs, so a doomed invocation fails immediately rather
    than after a potentially long read. Redirecting stdout to a file or
    another process is unaffected, since neither is a TTY.
    """
    if sys.stdout.isatty():
        print(
            "Error: refusing to write binary content to a terminal. "
            "Redirect stdout to a file or pipe, or use --file.",
            file=sys.stderr,
        )
        sys.exit(1)


async def read_blob(args: argparse.Namespace) -> None:
    """Read a blob via the Walrus HTTP aggregator and write its content to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_blob` subcommand arguments.
    """
    _refuse_tty_stdout()
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=ReadBlob(blob_id=args.blob_id))
    if not result.is_ok():
        print(f"Error reading blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: BlobData = result.result_data
    sys.stdout.buffer.write(data.content)


async def read_quilt(args: argparse.Namespace) -> None:
    """Read a single quilt patch via the Walrus HTTP aggregator and write it to stdout.

    Two mutually exclusive addressing modes: ``--patch-id`` alone, or
    ``--quilt-id``/``--patch-key`` together. Argparse can't express this
    pair-vs-single shape as a single mutually exclusive group, so it's
    validated here instead.

    Args:
        args (argparse.Namespace): Parsed `read_quilt` subcommand arguments.
    """
    _refuse_tty_stdout()
    if args.patch_id and (args.quilt_id or args.patch_key):
        print(
            "Error: --patch-id is not combined with --quilt-id/--patch-key.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not args.patch_id and not (args.quilt_id and args.patch_key):
        print(
            "Error: provide either --patch-id, or both --quilt-id and "
            "--patch-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = config_from_args(args)
    command = (
        ReadQuiltPatchById(patch_id=args.patch_id)
        if args.patch_id
        else ReadQuiltPatch(quilt_id=args.quilt_id, patch_key=args.patch_key)
    )
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=command)
    if not result.is_ok():
        print(f"Error reading quilt patch: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: QuiltPatch = result.result_data
    sys.stdout.buffer.write(data.content)


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
        result = await client.execute(command=ReadQuiltPatches(quilt_id=quilt_id))
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


async def read_blob_native(args: argparse.Namespace) -> None:
    """Reconstruct a blob from storage-node slivers, bypassing the aggregator.

    Content goes to ``--file`` when one is given, otherwise to stdout. The
    summary line is printed ONLY in the ``--file`` case: when the content
    itself is on stdout, anything else written there would corrupt it for a
    caller piping the output into a file or another process.

    Args:
        args (argparse.Namespace): Parsed `read_blob_native` subcommand
            arguments.
    """
    if args.file is None:
        _refuse_tty_stdout()
    config = config_from_args(args)
    blob_id = blob_id_from_url_base64(value=args.blob_id)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            result = await _read_blob_native_pipeline(
                client=client, blob_id=blob_id, verify=args.verify
            )
        except NativeReadError as exc:
            print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
            sys.exit(1)
        except (
            BlobDecodeError,
            RuntimeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            print(f"Error reading blob: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.file is None:
        sys.stdout.buffer.write(result.content)
        return

    await asyncio.to_thread(write_file_bytes, args.file, result.content)
    print(
        f"Wrote {len(result.content)} bytes to {args.file} "
        f"(epoch {result.epoch}, {result.slivers_used} {result.axis} slivers)"
    )
