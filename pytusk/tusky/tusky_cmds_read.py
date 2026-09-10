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
from pathlib import Path

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
    QuiltPatchItem,
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
    """Read a blob via the Walrus HTTP aggregator.

    Content goes to ``--file`` when one is given, otherwise to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_blob` subcommand arguments.
    """
    if args.file is None:
        _refuse_tty_stdout()
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=ReadBlob(blob_id=args.blob_id))
    if not result.is_ok():
        print(f"Error reading blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: BlobData = result.result_data
    if args.file is None:
        sys.stdout.buffer.write(data.content)
        return
    await asyncio.to_thread(write_file_bytes, args.file, data.content)
    print(f"Wrote {len(data.content)} bytes to {args.file}")


def _safe_out_name(identifier: str) -> str:
    """Sanitize a patch key/ID into a safe filename for --out-dir.

    Patch keys and tag values come from the quilt's own stored content,
    written by whoever stored it -- not from this reader. Taking only the
    final path component strips any directory traversal a crafted key
    could otherwise smuggle into a written filename.

    Args:
        identifier (str): The patch key or patch ID to sanitize.

    Returns:
        str: A filename with no directory components. Empty or dot-only
            input becomes "patch" so a write is never attempted with an
            unusable name.
    """
    name = Path(identifier).name
    if not name or name in (".", ".."):
        return "patch"
    return name


def _parse_tag(raw: str) -> tuple[str, str]:
    """Split a --tag KEY=VALUE argument into its key and value.

    Args:
        raw (str): Raw --tag argument text.

    Returns:
        tuple[str, str]: (key, value).
    """
    if "=" not in raw:
        print(f"Error: --tag {raw!r} is not KEY=VALUE.", file=sys.stderr)
        sys.exit(1)
    key, _, value = raw.partition("=")
    return key, value


async def read_quilt(args: argparse.Namespace) -> None:
    """Read one or more quilt patches via the Walrus HTTP aggregator.

    A single resolved patch goes to ``--file`` when given, otherwise
    stdout. More than one resolved patch requires ``--out-dir``, writing
    each to its own file named by patch key (or patch ID, when no key is
    known). Two addressing modes: repeatable ``--patch-id`` alone, or
    ``--quilt-id`` with one or more of repeatable ``--patch-key``/``--tag``.
    Argparse can't express this pair-vs-single, mutually-exclusive-mode
    shape as argument groups, so it's validated here instead.

    Args:
        args (argparse.Namespace): Parsed `read_quilt` subcommand arguments.
    """
    patch_ids: list[str] = args.patch_ids or []
    patch_keys: list[str] = args.patch_keys or []
    tags: list[str] = args.tags or []

    if patch_ids and (args.quilt_id or patch_keys or tags):
        print(
            "Error: --patch-id is not combined with "
            "--quilt-id/--patch-key/--tag.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not patch_ids and not (args.quilt_id and (patch_keys or tags)):
        print(
            "Error: provide either --patch-id (repeatable), or "
            "--quilt-id with at least one --patch-key/--tag.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.file is not None and args.out_dir is not None:
        print("Error: --file is not combined with --out-dir.", file=sys.stderr)
        sys.exit(1)

    config = config_from_args(args)
    requests: list[tuple[str, object]] = []
    async with WalrusClient(pytusk_config=config) as client:
        if patch_ids:
            requests = [
                (patch_id, ReadQuiltPatchById(patch_id=patch_id))
                for patch_id in patch_ids
            ]
        else:
            requests = [
                (
                    patch_key,
                    ReadQuiltPatch(quilt_id=args.quilt_id, patch_key=patch_key),
                )
                for patch_key in patch_keys
            ]
            if tags:
                listing_result = await client.execute(
                    command=ReadQuiltPatches(quilt_id=args.quilt_id)
                )
                if not listing_result.is_ok():
                    print(
                        "Error listing quilt patches: "
                        f"{listing_result.result_string}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                listing: QuiltPatchListing = listing_result.result_data
                wanted = {_parse_tag(t) for t in tags}
                seen_keys = {key for key, _ in requests}
                item: QuiltPatchItem
                for item in listing.patches:
                    matches = any(
                        item.tags.get(key) == value for key, value in wanted
                    )
                    if matches and item.patch_key not in seen_keys:
                        requests.append(
                            (
                                item.patch_key,
                                ReadQuiltPatchById(patch_id=item.patch_id),
                            )
                        )
                        seen_keys.add(item.patch_key)

        if not requests:
            print(
                "Error: no patches matched the given --tag selector(s).",
                file=sys.stderr,
            )
            sys.exit(1)
        if len(requests) > 1 and args.out_dir is None:
            print(
                "Error: more than one patch was requested; use --out-dir.",
                file=sys.stderr,
            )
            sys.exit(1)
        if len(requests) == 1 and args.out_dir is None and args.file is None:
            _refuse_tty_stdout()

        results: list[tuple[str, bytes]] = []
        names_seen: dict[str, str] = {}
        for identifier, command in requests:
            result = await client.execute(command=command)
            if not result.is_ok():
                print(
                    f"Error reading quilt patch {identifier}: "
                    f"{result.result_string}",
                    file=sys.stderr,
                )
                sys.exit(1)
            data: QuiltPatch = result.result_data
            if args.out_dir is not None:
                name = _safe_out_name(identifier)
                if name in names_seen:
                    print(
                        f"Error: patches {names_seen[name]!r} and "
                        f"{identifier!r} both sanitize to output filename "
                        f"{name!r}.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                names_seen[name] = identifier
            results.append((identifier, data.content))

    if args.out_dir is None:
        _, content = results[0]
        if args.file is None:
            sys.stdout.buffer.write(content)
            return
        await asyncio.to_thread(write_file_bytes, args.file, content)
        print(f"Wrote {len(content)} bytes to {args.file}")
        return

    await asyncio.to_thread(args.out_dir.mkdir, parents=True, exist_ok=True)
    for identifier, content in results:
        target = args.out_dir / _safe_out_name(identifier)
        await asyncio.to_thread(write_file_bytes, target, content)
        print(f"Wrote {len(content)} bytes to {target}")


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
