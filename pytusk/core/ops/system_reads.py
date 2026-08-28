#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Client-driven package-ID resolution and transaction-finality polling.

Both helpers here need a :class:`~pytusk.client.walrus_client.WalrusClient`
to make single-shot chain reads -- neither composes or submits a PTB, so
they sit in :mod:`pytusk.core.ops` rather than a ``*_compose.py`` module.
They also cannot live in :mod:`pytusk.core.chain` alongside
:mod:`pytusk.core.chain.committee`: that package is imported BY
``WalrusClient`` and must stay client-free, while these two are typed
against ``WalrusClient`` itself -- putting them in the same package would
be a real import cycle, not just a layering smell.
"""

import asyncio

from pysui import GetObject, GetTransaction

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.chain.committee import protobuf_json_to_python

__all__ = [
    "DEFAULT_FINALITY_MAX_ATTEMPTS",
    "DEFAULT_FINALITY_MAX_DELAY",
    "resolve_package_id",
    "wait_for_finality",
]

DEFAULT_FINALITY_MAX_ATTEMPTS: int = 10
"""Fallback poll count for :func:`wait_for_finality`.

Used when a caller does not pass ``finality_max_attempts``.
"""

DEFAULT_FINALITY_MAX_DELAY: float = 1.0
"""Fallback per-attempt delay ceiling, in seconds, for :func:`wait_for_finality`.

Backoff doubles from an eighth of this value and is clamped here, so the
default budget is roughly 7 seconds across
:data:`DEFAULT_FINALITY_MAX_ATTEMPTS` attempts.
"""


async def resolve_package_id(*, client: WalrusClient, system_object: str) -> str:
    """Read the current Walrus package ID from the configured System object.

    ``pytusk.tusky.tusky_cmds_common``'s ``walrus_package_id`` delegates to
    this function directly; the same pattern is also used by
    ``_ensure_wal``/``_cleanup_blobs`` in
    ``tests/integration_tests/conftest.py``: the package ID is read from
    ``System.package_id`` rather than assumed from a Blob's type-tag address,
    since the type-tag address can go stale after a package upgrade.

    Args:
        client (WalrusClient): Client used to fetch the System object.
        system_object (str): Object ID of the configured Walrus System object.

    Returns:
        str: The current Walrus package ID.

    Raises:
        RuntimeError: If the System object cannot be fetched.
        ValueError: If the fetched object carries no JSON view, or its JSON
            view has no ``package_id`` field -- an incomplete RPC response.
            The original hand-walked read had no such guard and crashed with
            a bare ``AttributeError`` on a malformed System object; this
            check is a deliberate improvement, not a preserved behaviour.
    """
    result = await client.execute(command=GetObject(object_id=system_object))
    if not result.is_ok():
        raise RuntimeError(
            f"Cannot fetch System object {system_object}: {result.result_string}"
        )
    fields = protobuf_json_to_python(value=result.result_data.json)
    if not isinstance(fields, dict):
        # ValueError, not TypeError -- mirrors the malformed-object
        # contract used throughout pytusk.core.chain.blob_fields and
        # pytusk.core.ops.storage_reads.
        raise ValueError(  # noqa: TRY004
            f"System object {system_object} has no JSON view; "
            "cannot resolve package_id."
        )
    package_id = fields.get("package_id")
    if not isinstance(package_id, str) or not package_id:
        raise ValueError(
            f"System object {system_object} is missing its 'package_id' field."
        )
    return package_id


async def wait_for_finality(
    *,
    client: WalrusClient,
    digest: str,
    max_attempts: int = DEFAULT_FINALITY_MAX_ATTEMPTS,
    max_delay: float = DEFAULT_FINALITY_MAX_DELAY,
) -> bool:
    """Poll ``GetTransaction`` until ``digest`` is visible in a checkpoint.

    A successful ``ExecuteTransaction`` means the transaction was accepted,
    NOT that its effects are readable yet. Reading a newly created object
    before the transaction lands in a checkpoint returns a stub -- an
    ``Object`` with no ``object_id`` and no JSON view -- from an RPC call
    that still reports ``is_ok()``. Callers that read back objects created
    by a transaction must wait on this function first, or they will
    misread that race as a malformed response.

    ``GetTransaction`` returns ``None`` while the digest is unfound, which
    is the poll signal. Delay starts at an eighth of ``max_delay`` and
    doubles per attempt, clamped at ``max_delay``.

    Args:
        client (WalrusClient): Client used to query the transaction.
        digest (str): Transaction digest to wait on, as returned by
            ``ExecuteTransaction``.
        max_attempts (int): Maximum number of polls before giving up.
            Defaults to :data:`DEFAULT_FINALITY_MAX_ATTEMPTS`.
        max_delay (float): Ceiling, in seconds, on the delay between
            polls. Defaults to :data:`DEFAULT_FINALITY_MAX_DELAY`.

    Returns:
        bool: ``True`` once the digest is visible, ``False`` if it was
            still not visible after ``max_attempts`` polls. A ``False``
            return is not proof the transaction failed -- the caller
            already knows it succeeded -- only that it had not become
            readable within the budget.
    """
    delay = max_delay / 8
    for attempt in range(max_attempts):
        result = await client.execute(command=GetTransaction(digest=digest))
        if (
            result.is_ok()
            and result.result_data is not None
            and result.result_data.checkpoint
        ):
            return True
        if attempt + 1 < max_attempts:
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
    return False
