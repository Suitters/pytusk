#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for local quilt assembly against pinned reference vectors.

Every blob id below was produced by the walrus CLI 1.54.0 (testnet context,
1000 shards) via ``store-quilt --dry-run --json``, which assembles the quilt
and reports the id WITHOUT writing anything on chain. They are the
interoperability contract: a quilt this SDK assembles must encode to the same
blob id as the reference implementation's, or a reader cannot find its
patches.

Do not regenerate these casually. If one starts failing, the question is
whether pytusk changed or the format did -- and the CLI is the arbiter, not
this file.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from pytusk.core.encoding.quilt import (
    QuiltAssemblyError,
    assemble_quilt,
    sorted_tag_entries,
)
from pytusk.core.encoding.redstuff import encode_blob
from pytusk.core.ops import ChainContext
from pytusk.core.pipelines import write as pipelines_write
from pytusk.core.pipelines.write import prepare_quilt_write
from pytusk.core.types import ChainContextError, NativeUploadError
from pytusk.core.types.quilts import QuiltPatchInput

N_SHARDS = 1000

# Every vector below assembles to this same length: the geometry is fixed by
# the shard count, not by how much the patches actually hold.
ASSEMBLED_LENGTH = 445556

SIX_A = b"A" * 6
THREE_HUNDRED_NUL = b"\x00" * 300


def blob_id_for(*, patches):
    """Assemble the patches and return the resulting blob id."""
    assembled = assemble_quilt(patches=patches, n_shards=N_SHARDS)
    assert len(assembled.data) == ASSEMBLED_LENGTH
    return encode_blob(data=assembled.data, n_shards=N_SHARDS).blob_id_base64


class TestReferenceVectors:
    """Pin assembled quilts against walrus CLI 1.54.0 output."""

    def test_single_patch_no_tags(self):
        """The minimal quilt: one ASCII identifier, no tags."""
        assert (
            blob_id_for(
                patches=[QuiltPatchInput(identifier="alpha.txt", contents=SIX_A)]
            )
            == "phXaTRM7PLJtNMowr-thRJ7Gqw99X0HIjMN218nFPUQ"
        )

    def test_mixed_length_tag_keys(self):
        """LOAD-BEARING: the only vector that catches str-ordered tags.

        ``"b"`` serializes to ``01 62`` and ``"aa"`` to ``02 6161``, so BCS
        orders ``b`` FIRST while ``sorted()`` on the strings orders ``aa``
        first. Every other vector in this file passes under either rule --
        this one does not, which is why it exists.
        """
        assert (
            blob_id_for(
                patches=[
                    QuiltPatchInput(
                        identifier="alpha.txt",
                        contents=SIX_A,
                        tags={"aa": "1", "b": "2"},
                    )
                ]
            )
            == "M15eRjEnstjqenyAOT1BPkulw9XyqMHN7m-rnCmhYYE"
        )

    def test_tags_normalized_from_caller_order(self):
        """Tags given out of order still land in canonical order."""
        assert (
            blob_id_for(
                patches=[
                    QuiltPatchInput(
                        identifier="alpha.txt",
                        contents=SIX_A,
                        tags={"zeta": "last", "alpha": "first"},
                    )
                ]
            )
            == "ckRssT8Ys1ifD_OeEhDYikngc2u_WgGUaRQaMV6QADA"
        )

    def test_non_ascii_identifier(self):
        """A multi-byte identifier is measured in UTF-8 bytes, not characters."""
        assert (
            blob_id_for(
                patches=[
                    QuiltPatchInput(identifier="café-notes.txt", contents=SIX_A)
                ]
            )
            == "MMH0HvblEzp2J1gSjvUM8x85X_USkI1TFiWU0yMl7Do"
        )

    def test_two_patches(self):
        """Two patches, each occupying one column after the index."""
        assert (
            blob_id_for(
                patches=[
                    QuiltPatchInput(identifier="alpha.txt", contents=SIX_A),
                    QuiltPatchInput(
                        identifier="bravo.txt", contents=THREE_HUNDRED_NUL
                    ),
                ]
            )
            == "62O_PdRdI8hMM7n4EajY6IpScAS4J0U1iUqZdgzGhEQ"
        )

    def test_patches_are_sorted_not_caller_ordered(self):
        """Packing order is a function of the batch, not of the call.

        The same two blobs handed over in the opposite order must produce the
        same quilt, or a patch id would depend on how a caller happened to
        list its inputs.
        """
        reversed_order = blob_id_for(
            patches=[
                QuiltPatchInput(identifier="bravo.txt", contents=THREE_HUNDRED_NUL),
                QuiltPatchInput(identifier="alpha.txt", contents=SIX_A),
            ]
        )
        assert reversed_order == "62O_PdRdI8hMM7n4EajY6IpScAS4J0U1iUqZdgzGhEQ"

    def test_column_ranges(self):
        """The index takes column zero; patches follow in identifier order."""
        assembled = assemble_quilt(
            patches=[
                QuiltPatchInput(identifier="alpha.txt", contents=SIX_A),
                QuiltPatchInput(identifier="bravo.txt", contents=THREE_HUNDRED_NUL),
            ],
            n_shards=N_SHARDS,
        )
        ranges = [
            (layout.identifier, layout.start_index, layout.end_index)
            for layout in assembled.patches
        ]
        assert ranges == [("alpha.txt", 1, 2), ("bravo.txt", 2, 3)]


class TestTagOrdering:
    """BCS orders map entries by their SERIALIZED key bytes."""

    @pytest.mark.parametrize(
        ("tags", "expected"),
        [
            # The ULEB128 length prefix leads, so the shorter key wins.
            ({"aa": "1", "b": "2"}, ["b", "aa"]),
            ({"zeta": "last", "alpha": "first"}, ["zeta", "alpha"]),
            # Equal-length keys fall through to content order, which is where
            # str-sorting and BCS agree -- and why the bug hid for so long.
            ({"bb": "1", "aa": "2"}, ["aa", "bb"]),
            ({}, []),
        ],
    )
    def test_key_order(self, tags, expected):
        """Entries come back in canonical BCS map order."""
        assert [entry.key for entry in sorted_tag_entries(tags=tags)] == expected

    def test_caller_order_is_irrelevant(self):
        """The same tag set serializes identically however it was built."""
        one = sorted_tag_entries(tags={"aa": "1", "b": "2"})
        other = sorted_tag_entries(tags={"b": "2", "aa": "1"})
        assert [entry.key for entry in one] == [entry.key for entry in other]


class _FakeQuiltClient:
    """Minimal ``WalrusClient``-shaped fake for ``prepare_quilt_write``.

    Only ``.committee()`` is read on this path: the package read happens
    inside ``prepare_chain_context``, which these tests patch at that seam,
    so this fake needs no ``execute()``.
    """

    def __init__(self, *, n_shards=N_SHARDS):
        self._committee = SimpleNamespace(n_shards=n_shards)
        self.config = SimpleNamespace(
            network=SimpleNamespace(system_object="0xsystem")
        )

    async def committee(self):
        """Return a committee carrying only the shard count assembly needs."""
        return self._committee


async def _fake_chain_context(**kwargs):
    """Stand in for the chain reads, preserving the fake client's shard count."""
    client = kwargs["client"]
    return ChainContext(
        committee=await client.committee(),
        system_object="0xsystem",
        package_id="0xpkg",
    )


class TestPrepareQuiltWrite:
    """``prepare_quilt_write`` is the quilt path's counterpart to
    ``prepare_write``: chain context, then ASSEMBLE, then encode.

    That order is load-bearing rather than incidental -- assembly needs the
    committee's shard count, and encoding needs assembly's output -- so these
    pin the sequence and the stage names, not merely the happy path.
    """

    async def test_preamble_blob_id_matches_direct_assembly(self, monkeypatch):
        """The pipeline must reach the SAME blob id as assembling and encoding
        directly. The pinned CLI vectors above are only an interoperability
        contract if the pipeline actually travels that same path."""
        patches = [
            QuiltPatchInput(identifier="a.bin", contents=SIX_A),
            QuiltPatchInput(identifier="b.bin", contents=THREE_HUNDRED_NUL),
        ]
        monkeypatch.setattr(
            pipelines_write, "prepare_chain_context", _fake_chain_context
        )

        preamble = await prepare_quilt_write(
            client=_FakeQuiltClient(),
            patches=patches,
            error_type=NativeUploadError,
        )

        assert preamble.encoded.blob_id_base64 == blob_id_for(patches=patches)
        assert [patch.identifier for patch in preamble.patches] == ["a.bin", "b.bin"]
        assert preamble.assemble_duration >= 0
        assert preamble.encode_duration >= 0
        assert preamble.package_id == "0xpkg"
        assert preamble.system_object == "0xsystem"

    async def test_assembly_failure_raises_under_assemble_stage(self, monkeypatch):
        """A duplicate identifier is rejected during assembly, which is
        PRE-SPEND, so it must raise under the caller's own error family
        carrying the ``assemble`` stage -- not the ``encode`` stage that
        would otherwise follow it."""
        patches = [
            QuiltPatchInput(identifier="dup.bin", contents=SIX_A),
            QuiltPatchInput(identifier="dup.bin", contents=THREE_HUNDRED_NUL),
        ]
        monkeypatch.setattr(
            pipelines_write, "prepare_chain_context", _fake_chain_context
        )

        with pytest.raises(NativeUploadError) as excinfo:
            await prepare_quilt_write(
                client=_FakeQuiltClient(),
                patches=patches,
                error_type=NativeUploadError,
            )

        assert excinfo.value.stage == "assemble"
        assert isinstance(excinfo.value.__cause__, QuiltAssemblyError)

    async def test_chain_context_failure_keeps_its_own_stage(self, monkeypatch):
        """A chain-read failure carries its stage through into the caller's
        error family. ``prepare_quilt_write`` TRANSLATES the error, it does
        not relabel which read failed."""

        async def _failing(**kwargs):
            raise ChainContextError(message="boom", stage="committee")

        monkeypatch.setattr(pipelines_write, "prepare_chain_context", _failing)

        with pytest.raises(NativeUploadError) as excinfo:
            await prepare_quilt_write(
                client=_FakeQuiltClient(),
                patches=[QuiltPatchInput(identifier="a.bin", contents=SIX_A)],
                error_type=NativeUploadError,
            )

        assert excinfo.value.stage == "committee"


    @pytest.mark.parametrize("count", [1, 2, 5])
    async def test_every_input_patch_appears_in_the_preamble(
        self, monkeypatch, count
    ):
        """One layout per input, at any batch size.

        The receipt promises one entry per packed blob and a reader uses that
        to find its data, so this is pinned as a PROPERTY across sizes rather
        than at one fixed size the way the vector tests above are.
        """
        patches = [
            QuiltPatchInput(identifier=f"p{index}.bin", contents=bytes([index]) * 6)
            for index in range(count)
        ]
        monkeypatch.setattr(
            pipelines_write, "prepare_chain_context", _fake_chain_context
        )

        preamble = await prepare_quilt_write(
            client=_FakeQuiltClient(),
            patches=patches,
            error_type=NativeUploadError,
        )

        assert len(preamble.patches) == count
        assert {layout.identifier for layout in preamble.patches} == {
            patch.identifier for patch in patches
        }

    async def test_assembly_dropping_a_patch_raises_pre_spend(self, monkeypatch):
        """A short patch set means paid-for content nobody can address.

        Assembly does not drop patches today; this pins the guard that would
        catch it if it ever did, and pins that it fails PRE-SPEND rather than
        surfacing as a receipt missing entries.
        """
        patches = [
            QuiltPatchInput(identifier="a.bin", contents=SIX_A),
            QuiltPatchInput(identifier="b.bin", contents=THREE_HUNDRED_NUL),
        ]
        monkeypatch.setattr(
            pipelines_write, "prepare_chain_context", _fake_chain_context
        )

        real_assemble = pipelines_write.assemble_quilt

        def _drop_one(*, patches, n_shards):
            assembled = real_assemble(patches=patches, n_shards=n_shards)
            return dataclasses.replace(assembled, patches=assembled.patches[:1])

        monkeypatch.setattr(pipelines_write, "assemble_quilt", _drop_one)

        with pytest.raises(NativeUploadError) as excinfo:
            await prepare_quilt_write(
                client=_FakeQuiltClient(),
                patches=patches,
                error_type=NativeUploadError,
            )

        assert excinfo.value.stage == "assemble"
        assert "b.bin" in str(excinfo.value)
