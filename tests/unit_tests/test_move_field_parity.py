#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Behavioural tests for helpers that read Walrus Move object fields.

This file began as a DRIFT GUARD between deliberately duplicated parsers:
``pytusk.core.ops.blob_execute`` and ``pytusk.tusky.tusky_cmds_common`` each
carried their own copy of the blob-field parser, kept apart to preserve a
one-way dependency (``tusky`` depends on ``core``, not the reverse), and this
module asserted the two agreed on every case.

Both duplications are now gone. ``matches_wal_coin_type`` was consolidated
into what is now :mod:`pytusk.core.ops.coins`; the blob-field parser was
consolidated into :func:`~pytusk.core.chain.blob_fields.blob_deletable_and_end_epoch`
at Plan #28 step 11, once step 10 had moved the CLI's copy into ``core`` and
dissolved the dependency argument for keeping two.

With one implementation of each, a parity assertion would compare a function
to itself. What is kept here is the BEHAVIOURAL coverage those parity cases
carried -- concrete expected values and the error cases -- which remains
worth having and is not duplicated elsewhere for the value cases below.
"""

import pytest

from pytusk.core.chain import (
    blob_deletable_and_end_epoch,
)
from pytusk.core.ops.coins import (
    matches_wal_coin_type,
)


class _FakeValue:
    """Stand-in for a protobuf ``Value`` where only the set field exists."""

    def __init__(self, **fields: object) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _FakeStruct:
    """Stand-in for a protobuf ``Struct``."""

    def __init__(self, *, fields: dict[str, object]) -> None:
        self.fields = fields


class _FakeJson:
    """Stand-in for ``Object.json``."""

    def __init__(self, *, struct_value: _FakeStruct | None) -> None:
        self.struct_value = struct_value


class _FakeObject:
    """Stand-in for ``sui_prot.Object``, holding only what the helpers read."""

    def __init__(self, *, object_id: str, json: _FakeJson | None) -> None:
        self.object_id = object_id
        self.json = json


def _blob_object(
    *, end_epoch: float | None = 42.0, deletable: bool | None = True
) -> _FakeObject:
    """Build a fake Blob-shaped object, mirroring test_system_ops.py's helper."""
    fields: dict[str, object] = {}
    if end_epoch is not None:
        storage_fields: dict[str, object] = {
            "end_epoch": _FakeValue(number_value=end_epoch)
        }
        fields["storage"] = _FakeValue(struct_value=_FakeStruct(fields=storage_fields))
    if deletable is not None:
        fields["deletable"] = _FakeValue(bool_value=deletable)
    return _FakeObject(
        object_id="0xblob", json=_FakeJson(struct_value=_FakeStruct(fields=fields))
    )


class TestMatchesWalCoinType:
    """Behavioural cases for the single ``matches_wal_coin_type``.

    These began as a parity check against a ``tusky_cmds`` duplicate. That
    duplicate is gone, but the cases are kept -- they are still this
    helper's only direct unit test, and they now assert concrete expected
    values instead of merely that two copies agreed with each other.
    """

    @pytest.mark.parametrize(
        ("coin_type", "wal_coin_type", "expected"),
        [
            # Pinned (currently mainnet only): exact match, nothing else.
            ("0xabc::wal::WAL", "0xabc::wal::WAL", True),
            ("0xabc::wal::WAL", "0xdef::wal::WAL", False),
            ("0xabc::sui::SUI", "0xabc::wal::WAL", False),
            ("", "0xabc::wal::WAL", False),
            # Unpinned (e.g. testnet): substring fallback.
            ("0xabc::wal::WAL", "", True),
            ("0xabc::sui::SUI", "", False),
            ("", "", False),
        ],
    )
    def test_matches_expected(
        self, coin_type: str, wal_coin_type: str, expected: bool
    ) -> None:
        """Pinned networks match exactly; unpinned fall back to substring."""
        assert (
            matches_wal_coin_type(coin_type=coin_type, wal_coin_type=wal_coin_type)
            is expected
        )


class TestBlobFieldValues:
    """Concrete field values read from a Blob object's JSON view.

    The error cases these once asserted in parallel now live in
    ``test_system_ops.py``'s ``TestEndEpochAndDeletable``. What is kept here
    is the value coverage that has no counterpart there -- notably
    ``end_epoch=0``, which must be reported as the integer 0 and never
    confused with "absent".
    """

    @pytest.mark.parametrize(
        ("end_epoch", "deletable"),
        [(42.0, True), (100.0, False), (0.0, True)],
    )
    def test_reads_expected_values(self, end_epoch: float, deletable: bool) -> None:
        """Both fields are read back exactly as the object carries them."""
        obj = _blob_object(end_epoch=end_epoch, deletable=deletable)
        actual_deletable, actual_end_epoch = blob_deletable_and_end_epoch(
            obj=obj  # type: ignore[arg-type]
        )
        assert actual_end_epoch == int(end_epoch)
        assert actual_deletable is deletable
