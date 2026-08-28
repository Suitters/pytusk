#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Structural (read-only) contracts satisfied by the native and relay
upload receipt/timings dataclasses.

Nothing here changes :class:`~pytusk.core.types.receipts.NativeBlobReceipt`,
:class:`~pytusk.core.types.receipts.RelayBlobReceipt`,
:class:`~pytusk.core.types.receipts.StageTimings`, or
:class:`~pytusk.core.types.receipts.RelayStageTimings` -- those dataclasses
already satisfy the protocols below structurally, by virtue of their
existing fields, with no modification required.
"""

import typing

from pysui import SuiCommand, SuiRpcResult

from pytusk.commands.walrus_command import WalrusCommand


class ExecuteOnlyClient(typing.Protocol):
    """Structural type for functions that only need ``execute()`` off a
    client -- lets tests pass a minimal fake without casting it to the
    concrete :class:`~pytusk.client.walrus_client.WalrusClient`."""

    async def execute(
        self,
        *,
        command: WalrusCommand | SuiCommand,
        timeout: float | None = None,
        headers: dict | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult: ...


@typing.runtime_checkable
class StageTimingsProtocol(typing.Protocol):
    """The honest intersection of ``StageTimings`` and ``RelayStageTimings``.

    Only the two fields common to both timing dataclasses are exposed here
    (``encode`` and ``total``); the pipeline-specific stage fields (e.g.
    ``register_tx1`` vs. ``register_tip_tx``) are deliberately omitted --
    they have no counterpart on the other side and so are not part of the
    honest intersection.

    Every member is declared as a read-only ``@property``, never as a bare
    annotated attribute. ``typing.Protocol`` matches bare attributes
    INVARIANTLY: a bare ``total: float | None`` would reject an
    implementation whose ``total`` is narrower or wider in a way the
    checker cannot prove interchangeable both ways. Read-only properties,
    by contrast, are checked COVARIANTLY on their return type, so a
    concrete ``float`` return satisfies a protocol declared ``float |
    None`` with no widening of the concrete dataclass. Do not "simplify"
    these into bare attributes -- doing so would force widening
    ``StageTimings``/``RelayStageTimings``, which is explicitly out of
    scope for this contract.
    """

    @property
    def encode(self) -> float | None: ...

    @property
    def total(self) -> float | None: ...


@typing.runtime_checkable
class UploadReceipt(typing.Protocol):
    """Read-only contract satisfied by both
    :class:`~pytusk.core.types.receipts.NativeBlobReceipt` and
    :class:`~pytusk.core.types.receipts.RelayBlobReceipt`.

    Every member is declared as a read-only ``@property``, never as a bare
    annotated attribute, for the same covariance reason documented on
    :class:`StageTimingsProtocol`: several of
    ``NativeBlobReceipt``'s fields are narrower than this protocol's
    declared type (e.g. ``blob_id: str`` vs. this protocol's ``str |
    None``). A bare attribute is matched invariantly by type checkers and
    would reject that narrower field, forcing ``NativeBlobReceipt`` to be
    widened just to satisfy this contract -- which is explicitly forbidden.
    A read-only property is checked covariantly on its return type, so the
    narrower concrete type satisfies the wider protocol type with no
    change to the dataclass. Do not "simplify" these into bare attributes.
    """

    @property
    def certified(self) -> bool: ...

    @property
    def failed_stage(self) -> str | None: ...

    @property
    def blob_id(self) -> str | None: ...

    @property
    def object_id(self) -> str | None: ...

    @property
    def end_epoch(self) -> int | None: ...

    @property
    def timings(self) -> StageTimingsProtocol: ...

    @property
    def register_tx_digest(self) -> str | None: ...

    @property
    def certify_tx_digest(self) -> str | None: ...
