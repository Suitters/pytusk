#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Drift guard between the deliberately duplicated blob-field parsers.

``pytusk.core.system_ops`` and ``pytusk.tusky.tusky_cmds`` each carry their
own copy of the blob-field parser (``_end_epoch_and_deletable`` /
``_blob_deletable_and_end_epoch``). That duplication is DELIBERATE -- see
both functions' own docstrings -- to preserve a one-way dependency
(``tusky`` depends on ``core``, not the reverse), not an oversight to be
refactored away. What was previously missing is any automated signal that
the two copies have drifted apart. This module's job is to be that signal:
it imports BOTH copies directly and asserts they agree on every case. A
future Walrus contract upgrade that changes the Blob object's JSON field
layout is exactly the kind of change likely to touch only one copy by
accident -- this file turns that mistake into a failing test instead of a
silent divergence.

``_matches_wal_coin_type`` was duplicated the same way until the
``tusky_cmds`` copy was consolidated into :mod:`pytusk.core.utils` -- the
one-way rule forbids ``core`` importing ``tusky``, not the reverse. Its
parity check is therefore gone, but the behavioural cases that check
introduced are kept below: they remain that helper's only direct unit
test, and they now assert concrete expected values rather than mere
agreement between two copies.
"""

import pytest

from pytusk.core.system_ops import (
    _end_epoch_and_deletable,
)
from pytusk.core.utils import (
    _matches_wal_coin_type,
)
from pytusk.tusky.tusky_cmds_common import (
    _blob_deletable_and_end_epoch,
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
    """Behavioural cases for the single ``_matches_wal_coin_type``.

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
            _matches_wal_coin_type(coin_type=coin_type, wal_coin_type=wal_coin_type)
            is expected
        )


class TestEndEpochAndDeletableParity:
    """Drift guard for ``_end_epoch_and_deletable`` vs
    ``_blob_deletable_and_end_epoch``.

    The two copies DELIBERATELY return their fields in opposite tuple
    order (matching each function's own name: end_epoch-first vs
    deletable-first) -- every comparison below normalizes that before
    asserting equality, so this file is not itself broken by that
    intentional difference.
    """

    @pytest.mark.parametrize(
        ("end_epoch", "deletable"),
        [(42.0, True), (100.0, False), (0.0, True)],
    )
    def test_agrees_on_happy_path(self, end_epoch: float, deletable: bool) -> None:
        obj = _blob_object(end_epoch=end_epoch, deletable=deletable)
        core_result = _end_epoch_and_deletable(obj)  # type: ignore[arg-type]
        tusky_result = _blob_deletable_and_end_epoch(obj)  # type: ignore[arg-type]
        assert core_result == (tusky_result[1], tusky_result[0])

    def test_agrees_on_no_json_view_error(self) -> None:
        obj = _FakeObject(object_id="0xblob", json=None)
        with pytest.raises(ValueError):
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            _blob_deletable_and_end_epoch(obj)  # type: ignore[arg-type]

    def test_agrees_on_missing_storage_field_error(self) -> None:
        obj = _FakeObject(
            object_id="0xblob",
            json=_FakeJson(
                struct_value=_FakeStruct(
                    fields={"deletable": _FakeValue(bool_value=True)}
                )
            ),
        )
        with pytest.raises(ValueError):
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            _blob_deletable_and_end_epoch(obj)  # type: ignore[arg-type]

    def test_agrees_on_missing_end_epoch_error(self) -> None:
        obj = _FakeObject(
            object_id="0xblob",
            json=_FakeJson(
                struct_value=_FakeStruct(
                    fields={
                        "storage": _FakeValue(struct_value=_FakeStruct(fields={})),
                        "deletable": _FakeValue(bool_value=True),
                    }
                )
            ),
        )
        with pytest.raises(ValueError):
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            _blob_deletable_and_end_epoch(obj)  # type: ignore[arg-type]

    def test_agrees_on_missing_deletable_error(self) -> None:
        obj = _blob_object(deletable=None)
        with pytest.raises(ValueError):
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            _blob_deletable_and_end_epoch(obj)  # type: ignore[arg-type]
