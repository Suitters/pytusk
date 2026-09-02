#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Parser-level tests for the tusky CLI argument definitions.

Unlike test_tusky_cmds.py, which hand-builds argparse.Namespace objects to
test handler logic, these tests call build_parser() with real CLI argument
strings -- so a flag defined on the wrong subparser, a typo'd dest, or a
short-flag collision surfaces here instead of only at real CLI use.
"""

import pytest

from pytusk.tusky.tusky_args import build_parser


OBJECT_ID = "0x" + "1" * 64
OBJECT_ID_2 = "0x" + "2" * 64
STORAGE_ID = "0x" + "3" * 64


class TestObjectIdArguments:
    """-o/--object-id parses to dest object_id and validates format."""

    def test_blob_object_id_short_flag(self) -> None:
        """blob accepts -o as a Sui object ID."""
        args = build_parser(in_args=["blob", "-o", OBJECT_ID])
        assert args.subcommand == "blob"
        assert args.object_id == OBJECT_ID

    def test_blob_object_id_long_flag(self) -> None:
        """blob accepts --object-id as a Sui object ID."""
        args = build_parser(in_args=["blob", "--object-id", OBJECT_ID])
        assert args.object_id == OBJECT_ID

    def test_blob_object_id_rejects_invalid_format(self) -> None:
        """blob rejects a malformed object id."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["blob", "-o", "not-an-id"])

    def test_certify_blob_object_id(self) -> None:
        """certify_blob's -o parses to object_id."""
        args = build_parser(in_args=["certify_blob", "-o", OBJECT_ID])
        assert args.object_id == OBJECT_ID

    def test_extend_blob_expiration_object_id(self) -> None:
        """extend_blob_expiration's -o parses to object_id."""
        args = build_parser(
            in_args=["extend_blob_expiration", "-o", OBJECT_ID, "--epochs", "5"]
        )
        assert args.object_id == OBJECT_ID

    def test_delete_blob_object_id(self) -> None:
        """delete_blob's -o parses to object_id."""
        args = build_parser(in_args=["delete_blob", "-o", OBJECT_ID])
        assert args.object_id == OBJECT_ID

    def test_delete_blob_all_flag(self) -> None:
        """delete_blob's --all is the renamed flag (was --all-blobs)."""
        args = build_parser(in_args=["delete_blob", "--all"])
        assert args.all is True

    def test_delete_blob_requires_exactly_one_target(self) -> None:
        """delete_blob's target group rejects both -o and --all together."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["delete_blob", "-o", OBJECT_ID, "--all"])

    def test_delete_blob_requires_a_target(self) -> None:
        """delete_blob's target group is required."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["delete_blob"])


# A real testnet blob ID: 32-byte content hash, URL-safe base64, unpadded,
# hence exactly 43 characters. Placeholders no longer work here -- -b is
# validated by ValidateBlobID as of Plan #22, because blob_status makes a
# blob ID the key of a committee-wide query rather than just a read arg.
BLOB_ID = "8XiGin_tGIB1hpkJINtSgKToT0EFo3qdYMY4U9nBJK4"


class TestBlobIdArgument:
    """-b/--blob-id parses to dest blob_id, distinct from object_id."""

    def test_read_blob_blob_id_short_flag(self) -> None:
        """read_blob accepts -b as a Walrus blob ID (not object-id validated)."""
        args = build_parser(in_args=["read_blob", "-b", BLOB_ID])
        assert args.blob_id == BLOB_ID

    def test_read_blob_blob_id_long_flag(self) -> None:
        """read_blob accepts --blob-id."""
        args = build_parser(in_args=["read_blob", "--blob-id", BLOB_ID])
        assert args.blob_id == BLOB_ID

    def test_rejects_wrong_length(self) -> None:
        """A blob ID that is not 43 characters cannot be a 32-byte hash."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["read_blob", "-b", "somewalrusblobid"])

    def test_rejects_standard_base64_plus(self) -> None:
        """'+' belongs to the STANDARD alphabet, never the URL-safe one.

        This is the case length and byte-count checks alone do not catch:
        ``urlsafe_b64decode`` maps '-'/'_' onto '+'/'/' and then decodes
        with validate=False, so a '+' decodes cleanly to 32 bytes and
        would be accepted as a DIFFERENT blob ID than the one typed.
        """
        with pytest.raises(SystemExit):
            build_parser(in_args=["read_blob", "-b", BLOB_ID.replace("_", "+", 1)])

    def test_rejects_standard_base64_slash(self) -> None:
        """'/' is rejected for the same reason as '+'."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["read_blob", "-b", BLOB_ID.replace("_", "/", 1)])

    def test_rejects_too_long(self) -> None:
        """44 characters decodes to more than 32 bytes."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["read_blob", "-b", BLOB_ID + "A"])


class TestBurnBlobRepeatable:
    """burn_blob's -o/--object-id is repeatable and validates each value."""

    def test_burn_blob_single(self) -> None:
        """A single -o produces a one-item list."""
        args = build_parser(in_args=["burn_blob", "-o", OBJECT_ID])
        assert args.object_id == [OBJECT_ID]

    def test_burn_blob_repeated(self) -> None:
        """Repeating -o accumulates into a list, matching the pre-existing UX."""
        args = build_parser(in_args=["burn_blob", "-o", OBJECT_ID, "-o", OBJECT_ID_2])
        assert args.object_id == [OBJECT_ID, OBJECT_ID_2]

    def test_burn_blob_rejects_invalid_id(self) -> None:
        """Each repeated value is validated, not just the first."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["burn_blob", "-o", OBJECT_ID, "-o", "bad-id"])

    def test_burn_blob_requires_at_least_one(self) -> None:
        """-o/--object-id is required on burn_blob."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["burn_blob"])


class TestStorageIdArguments:
    """-s/--storage-id parses to dest storage_id."""

    def test_split_storage_storage_id(self) -> None:
        """split_storage's -s parses to storage_id."""
        args = build_parser(
            in_args=["split_storage", "-s", STORAGE_ID, "--by-epoch", "5"]
        )
        assert args.storage_id == STORAGE_ID

    def test_reclaim_storage_storage_id(self) -> None:
        """reclaim_storage's -s accepts one or more storage ids."""
        args = build_parser(in_args=["reclaim_storage", "-s", STORAGE_ID])
        assert args.storage_id == [STORAGE_ID]

    def test_reclaim_storage_all_flag(self) -> None:
        """reclaim_storage's --all remains unchanged by the rename."""
        args = build_parser(in_args=["reclaim_storage", "--all"])
        assert args.all is True

    def test_reclaim_storage_requires_exactly_one_target(self) -> None:
        """reclaim_storage's target group rejects both -s and --all together."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["reclaim_storage", "-s", STORAGE_ID, "--all"])

    def test_extend_blob_with_storage_both_ids(self) -> None:
        """extend_blob_with_storage takes distinct -o and -s without collision."""
        args = build_parser(
            in_args=["extend_blob_with_storage", "-o", OBJECT_ID, "-s", STORAGE_ID]
        )
        assert args.object_id == OBJECT_ID
        assert args.storage_id == STORAGE_ID


class TestStoreQuiltPatchFlags:
    """--patch-file/--patch-content parse to dest patch_file/patch_content."""

    def test_patch_file_repeatable_key_value(self) -> None:
        """--patch-file KEY=PATH is repeatable and parses as (key, value) pairs."""
        args = build_parser(
            in_args=[
                "store_quilt",
                "--patch-file",
                "a=path/to/a",
                "--patch-file",
                "b=path/to/b",
                "--epochs",
                "5",
            ]
        )
        assert args.patch_file == [("a", "path/to/a"), ("b", "path/to/b")]

    def test_patch_content_repeatable_key_value(self) -> None:
        """--patch-content KEY=TEXT is repeatable and parses as (key, value) pairs."""
        args = build_parser(
            in_args=["store_quilt", "--patch-content", "a=hello", "--epochs", "5"]
        )
        assert args.patch_content == [("a", "hello")]

    def test_store_blob_content_flag_unaffected(self) -> None:
        """store_blob's own --content stays a plain single string, not renamed."""
        args = build_parser(in_args=["store_blob", "--content", "hello", "--epochs", "5"])
        assert args.content == "hello"


class TestQuiltPatches:
    """quilt_patches requires exactly one of -b/--blob-id or -o/--object-id."""

    def test_blob_id_flag(self) -> None:
        """-b/--blob-id parses to dest blob_id."""
        args = build_parser(
            in_args=[
                "quilt_patches",
                "--blob-id",
                "A" * 43,
            ]
        )
        assert args.blob_id == "A" * 43

    def test_blob_id_short_flag(self) -> None:
        """-b parses to the same dest blob_id."""
        args = build_parser(in_args=["quilt_patches", "-b", "A" * 43])
        assert args.blob_id == "A" * 43

    def test_object_id_flag(self) -> None:
        """-o/--object-id parses to dest object_id."""
        args = build_parser(in_args=["quilt_patches", "-o", OBJECT_ID])
        assert args.object_id == OBJECT_ID

    def test_missing_both_rejected(self) -> None:
        """Omitting both -b and -o exits non-zero."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["quilt_patches"])

    def test_both_given_rejected(self) -> None:
        """Passing both -b and -o exits non-zero (mutually exclusive)."""
        with pytest.raises(SystemExit):
            build_parser(
                in_args=["quilt_patches", "-b", "A" * 43, "-o", OBJECT_ID]
            )


class TestTipGasSource:
    """--tip-gas-source parses to dest tip_gas_source (was --tip-source)."""

    def test_tip_gas_source_default(self) -> None:
        """Default is from_gas when --tip-gas-source is omitted."""
        args = build_parser(
            in_args=["store_blob_relay", "--content", "hello", "--epochs", "5"]
        )
        assert args.tip_gas_source == "from_gas"

    def test_tip_gas_source_explicit(self) -> None:
        """--tip-gas-source accepts an explicit coin object id."""
        args = build_parser(
            in_args=[
                "store_blob_relay",
                "--content",
                "hello",
                "--epochs",
                "5",
                "--tip-gas-source",
                OBJECT_ID,
            ]
        )
        assert args.tip_gas_source == OBJECT_ID


class TestStoreBlobRelayLogVerbose:
    """--log-file/--verbose exist on BOTH relay commands, as on store_blob_native."""

    def test_log_file_and_verbose(self, tmp_path) -> None:
        """store_blob_relay accepts --log-file and --verbose like store_blob_native."""
        log_path = tmp_path / "run.log"
        args = build_parser(
            in_args=[
                "store_blob_relay",
                "--content",
                "hello",
                "--epochs",
                "5",
                "--log-file",
                str(log_path),
                "--verbose",
            ]
        )
        assert args.log_file == log_path
        assert args.verbose is True

    def test_quilt_relay_log_file_and_verbose(self, tmp_path) -> None:
        """store_quilt_relay accepts --log-file and --verbose as well.

        The relay upload progress they surface is emitted by the shared
        upload stage, so the quilt command has exactly the same thing to
        report as the blob command does.
        """
        log_path = tmp_path / "quilt.log"
        args = build_parser(
            in_args=[
                "store_quilt_relay",
                "--patch-content",
                "a.txt=hello",
                "--epochs",
                "5",
                "--log-file",
                str(log_path),
                "--verbose",
            ]
        )
        assert args.log_file == log_path
        assert args.verbose is True


class TestRecipientAndAddressValidation:
    """--recipient/--address now validate as Sui addresses where they didn't before."""

    def test_store_blob_recipient_rejects_invalid_address(self) -> None:
        """store_blob's --recipient now validates format."""
        with pytest.raises(SystemExit):
            build_parser(
                in_args=[
                    "store_blob",
                    "--content",
                    "hello",
                    "--epochs",
                    "5",
                    "--recipient",
                    "not-an-address",
                ]
            )

    def test_expiry_report_address_rejects_invalid_address(self) -> None:
        """expiry_report's --address now validates format."""
        with pytest.raises(SystemExit):
            build_parser(in_args=["expiry_report", "--address", "not-an-address"])


class TestFuseStorageUnchanged:
    """--fuse-to/--fuse-from flags stay as-is; only help text changed in Stage 1."""

    def test_fuse_to_and_fuse_from(self) -> None:
        """fuse_storage's flags still parse under their original names."""
        args = build_parser(
            in_args=["fuse_storage", "--fuse-to", STORAGE_ID, "--fuse-from", OBJECT_ID]
        )
        assert args.fuse_to == STORAGE_ID
        assert args.fuse_from == [OBJECT_ID]


class TestStoreQuiltRelayArgs:
    """store_quilt_relay takes the quilt's patch flags AND the relay flags.

    It is a separate subcommand rather than a flag on store_quilt, so the
    two flag families have to meet on it for the first time -- that is what
    these pin.
    """

    def test_patch_flags_parse(self) -> None:
        """The three patch sources parse to the same dests store_quilt uses."""
        args = build_parser(
            in_args=[
                "store_quilt_relay",
                "--patch-file",
                "a=path/to/a",
                "--patch-content",
                "b=hello",
                "--epochs",
                "5",
            ]
        )
        assert args.subcommand == "store_quilt_relay"
        assert args.patch_file == [("a", "path/to/a")]
        assert args.patch_content == [("b", "hello")]
        assert args.paths == []

    def test_relay_flags_parse(self) -> None:
        """The relay knobs land on the same dests store_blob_relay uses."""
        args = build_parser(
            in_args=[
                "store_quilt_relay",
                "--patch-content",
                "a=hello",
                "--epochs",
                "5",
                "--relay",
                "myrelay",
                "--max-tip",
                "1000",
                "--timeout",
                "900",
            ]
        )
        assert args.relay == "myrelay"
        assert args.max_tip == 1000
        assert args.timeout == 900.0
        assert args.tip_gas_source == "from_gas"

    def test_defaults_match_the_blob_relay_command(self) -> None:
        """Shared knobs default identically on both relay subcommands, so a
        caller switching from blobs to quilts is not silently given
        different behaviour."""
        quilt = build_parser(
            in_args=["store_quilt_relay", "--patch-content", "a=x", "--epochs", "5"]
        )
        blob = build_parser(
            in_args=["store_blob_relay", "--content", "x", "--epochs", "5"]
        )
        for field in (
            "mode",
            "relay",
            "tip_gas_source",
            "max_tip",
            "timeout",
            "recipient",
            "permanent",
            "full_json",
            "sender",
            "sponsor",
        ):
            assert getattr(quilt, field) == getattr(blob, field), field

    def test_dispatch_table_has_a_handler(self) -> None:
        """A subcommand the parser accepts but the dispatch table does not
        know is a command that parses and then dies -- the failure mode this
        pins against."""
        from pytusk.tusky.tusky import _DISPATCH

        assert "store_quilt_relay" in _DISPATCH

