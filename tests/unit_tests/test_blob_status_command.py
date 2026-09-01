#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for GetBlobStatus request shape and response parsing."""

import httpx
import pytest

from pytusk.commands.node_commands import GetBlobStatus
from pytusk.core.types.blob_status import (
    DeletableStatus,
    InvalidStatus,
    NonexistentStatus,
    PermanentStatus,
)

_BLOB_ID = bytes(range(32))

# Captured verbatim from a testnet storage node, 2026-09-01 (committee
# epoch 507). Four distinct nodes returned byte-identical bodies.
_LIVE_PERMANENT = {
    "success": {
        "code": 200,
        "data": {
            "permanent": {
                "endEpoch": 508,
                "isCertified": True,
                "statusEvent": {
                    "txDigest": "DaRmjvMBZoU6cU1iZUM3qMdakFYa5jJcmWFyaxW2aReN",
                    "eventSeq": "0",
                },
                "deletableCounts": {
                    "count_deletable_total": 0,
                    "count_deletable_certified": 0,
                },
                "initialCertifiedEpoch": 507,
            }
        },
    }
}

# SOURCE-DERIVED, NEVER OBSERVED LIVE. An invalid blob cannot be produced
# on demand, so this fixture is built from the Rust enum declaration in
# crates/walrus-storage-node-client/src/api.rs:39-79. Note the field is
# `event`, NOT `statusEvent` -- `Invalid` names it differently from
# `Permanent`, and camelCase of a single word is a no-op.
_SOURCE_DERIVED_INVALID = {
    "success": {
        "code": 200,
        "data": {
            "invalid": {
                "event": {"txDigest": "AbC", "eventSeq": "7"},
            }
        },
    }
}


def _parse(body: object, *, status_code: int = 200):
    """Run GetBlobStatus.parse_response over a JSON body."""
    return GetBlobStatus(blob_id=_BLOB_ID).parse_response(
        httpx.Response(status_code, json=body)
    )


class TestRequestShape:
    """URL and method, which the committee fan-out depends on."""

    def test_method_is_get(self) -> None:
        """Status is a read."""
        assert GetBlobStatus(blob_id=_BLOB_ID).http_method() == "GET"

    def test_url_uses_url_safe_unpadded_base64(self) -> None:
        """The path carries the URL-safe alphabet, never the standard one."""
        path = GetBlobStatus(blob_id=_BLOB_ID).url_path("http://node:9185")
        assert path.endswith("/status")
        assert "+" not in path and "/v1/blobs/" in path
        assert "=" not in path.rsplit("/", 2)[1]


class TestLiveCapture:
    """The one variant confirmed against real nodes."""

    def test_permanent_parses(self) -> None:
        """Every field of the live permanent body round-trips."""
        result = _parse(_LIVE_PERMANENT)
        assert result.is_ok()
        status = result.result_data
        assert isinstance(status, PermanentStatus)
        assert status.end_epoch == 508
        assert status.is_certified is True
        assert status.initial_certified_epoch == 507
        assert status.deletable_counts.total == 0

    def test_event_seq_string_becomes_int(self) -> None:
        """The wire sends eventSeq as a STRING; callers get an int."""
        status = _parse(_LIVE_PERMANENT).result_data
        assert status.status_event.event_seq == 0
        assert isinstance(status.status_event.event_seq, int)


class TestVariants:
    """All four protocol variants, including the bare-string unit variant."""

    def test_nonexistent_is_a_bare_string(self) -> None:
        """An externally tagged UNIT variant is a string, not an object.

        Assuming an object here would break on a perfectly valid response.
        """
        result = _parse({"success": {"code": 200, "data": "nonexistent"}})
        assert result.is_ok()
        assert isinstance(result.result_data, NonexistentStatus)

    def test_deletable(self) -> None:
        """Deletable carries counts and no end_epoch."""
        result = _parse(
            {
                "success": {
                    "code": 200,
                    "data": {
                        "deletable": {
                            "deletableCounts": {
                                "count_deletable_total": 3,
                                "count_deletable_certified": 2,
                            },
                            "initialCertifiedEpoch": None,
                        }
                    },
                }
            }
        )
        assert result.is_ok()
        status = result.result_data
        assert isinstance(status, DeletableStatus)
        assert (status.deletable_counts.total, status.deletable_counts.certified) == (3, 2)
        assert status.initial_certified_epoch is None

    def test_invalid_uses_event_not_status_event(self) -> None:
        """Source-derived fixture -- see the module-level note."""
        result = _parse(_SOURCE_DERIVED_INVALID)
        assert result.is_ok()
        status = result.result_data
        assert isinstance(status, InvalidStatus)
        assert status.status_event.event_seq == 7

    def test_deletable_counts_children_are_snake_case(self) -> None:
        """Mixed casing is real: camelCase key, snake_case children.

        DeletableCounts carries no serde rename attribute, so its fields
        keep their Rust names while every key around them is camelCase.
        A camelCase spelling here must NOT parse.
        """
        result = _parse(
            {
                "success": {
                    "code": 200,
                    "data": {
                        "deletable": {
                            "deletableCounts": {
                                "countDeletableTotal": 3,
                                "countDeletableCertified": 2,
                            },
                            "initialCertifiedEpoch": None,
                        }
                    },
                }
            }
        )
        assert not result.is_ok()


class TestFailuresAreReturnedNotRaised:
    """One bad node must never abort the committee fan-out."""

    @pytest.mark.parametrize(
        "label,body",
        [
            ("two variants", {"success": {"code": 200, "data": {"permanent": {}, "deletable": {}}}}),
            ("unknown variant", {"success": {"code": 200, "data": {"squishy": {}}}}),
            ("unknown string", {"success": {"code": 200, "data": "wat"}}),
            ("no envelope", {"permanent": {}}),
            ("payload not object", {"success": {"code": 200, "data": {"permanent": 5}}}),
            (
                "missing endEpoch",
                {"success": {"code": 200, "data": {"permanent": {"isCertified": True}}}},
            ),
        ],
    )
    def test_returns_failed_result(self, label: str, body: object) -> None:
        """Each malformed body yields a failed SuiRpcResult, not an exception."""
        result = _parse(body)
        assert not result.is_ok()
        assert result.result_string

    def test_non_integer_event_seq(self) -> None:
        """A non-numeric eventSeq is reported, not silently coerced."""
        result = _parse(
            {
                "success": {
                    "code": 200,
                    "data": {"invalid": {"event": {"txDigest": "A", "eventSeq": "nope"}}},
                }
            }
        )
        assert not result.is_ok()

    def test_http_error_is_reported(self) -> None:
        """A non-2xx response never reaches the envelope unwrap."""
        result = GetBlobStatus(blob_id=_BLOB_ID).parse_response(
            httpx.Response(500, text="boom")
        )
        assert not result.is_ok()
        assert "500" in result.result_string
