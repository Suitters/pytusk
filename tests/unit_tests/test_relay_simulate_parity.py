#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests closing the CLI-simulate/library-execute drift defect fixed
by step 6 of the ``refactor_layering_0.5.0`` refactor.

Before this step, ``tusky store_blob_relay --mode simulate`` hand-assembled
Tx1 itself instead of going through
:func:`~pytusk.core.ops.blob_compose.add_registration_sequence` and the
shared :func:`~pytusk.core.ops.blob_execute.preflight_sponsor` /
:func:`~pytusk.core.ops.blob_execute.preflight_payment` guards that
:func:`~pytusk.core.pipelines.write.store_blob_relay` (the library's
execute path) uses -- so the CLI's simulate branch had silently dropped the
tip-coin usability check and the sponsor-signability preflight, and could
report success where a real run would fail. This file pins two things:

1. ``TestRelaySimulateMatchesPipelineComposition`` -- the CLI simulate path
   and the library execute path compose IDENTICAL Tx1s for identical
   inputs. Both now call the SAME real (unmocked)
   :func:`~pytusk.core.ops.blob_compose.add_registration_sequence` (which
   itself calls the real, unmocked
   :func:`~pytusk.core.ops.tip_compose.add_tip` and
   :func:`~pytusk.core.ops.blob_compose.add_reserve_and_register`) against
   ``_RecordingComposeTxn``, the same recording-fake pattern
   ``test_system_ops.py``'s ``_RecordingTipTxn`` already established for
   this package: a REAL pysui ``ProgrammableTransactionBuilder`` backs
   ``txn.builder`` (so ``add_tip``'s input-0 guard and pure-input
   registration -- PTB input 0 -- are exercised for real, byte-exact), and
   ``move_call``/``split_coin``/``transfer_objects`` are recording stubs
   (no network round trip) appended to one ordered ``.calls`` log. A full
   real ``AsyncSuiTransaction`` was ruled out because its ``move_call``
   fetches the target Move function's normalized signature over the
   network (``_function_meta_args``), which a unit test cannot do without
   a live node. Comparing two independently-built txns'
   ``builder.inputs``/``.calls`` for identical inputs is this file's stand
   -in for comparing ``txn.builder.inputs``/``txn.builder.commands``. The
   weaker fallback the task description allows (asserting the CLI passed
   :func:`add_registration_sequence` the same kwargs the library did) was
   NOT needed -- this test exercises the real composition functions, not
   just their call sites.
2. ``TestRelaySimulateGuardsNowRun`` -- the two guards the CLI simulate
   path used to skip now actually run: an unusable tip coin and an
   unsignable sponsor both fail simulate the same way they would fail
   execute, instead of silently producing a simulate result a real run
   could never reach.
"""

import argparse
import types
from typing import Any

import pytest
from pysui import SuiRpcResult
from pysui.sui.sui_common.txn_transaction_builder import ProgrammableTransactionBuilder

from pytusk.core.encoding import EncodedBlob
from pytusk.core.ops import blob_execute as blob_execute_module
from pytusk.core.pipelines import registration as registration_module
from pytusk.core.pipelines import write as pipeline_module
from pytusk.core.relay_upload.common import TipQuote
from pytusk.core.types import FROM_GAS, ConstTip
from pytusk.core.types.tips import AuthPackage
from pytusk.tusky import tusky_cmds_relay


class _RecordingComposeTxn:
    """Recording fake for ``AsyncSuiTransaction``, mirroring
    ``test_system_ops.py``'s ``_RecordingTipTxn``: a REAL pysui
    ``ProgrammableTransactionBuilder`` backs ``.builder`` (so ``add_tip``'s
    input-0 guard and pure-input registration are exercised for real,
    byte-exact), plus recording ``split_coin``/``move_call``/
    ``transfer_objects`` stubs (no network round trip) appended to one
    ordered ``.calls`` log, in call order -- so the full Tx1 command
    sequence (tip commands interleaved with reserve/register/transfer) can
    be asserted as one timeline, exactly like ``test_system_ops.py``'s
    ``TestAddRegistrationSequenceCommandParity``.
    """

    def __init__(
        self, *, sender: str | None = None, sponsor: str | None = None, gas: str = "GAS-SENTINEL"
    ) -> None:
        self.builder = ProgrammableTransactionBuilder()
        self.sender = sender
        self.sponsor = sponsor
        self.gas = gas
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._next_result = 0

    def _result(self) -> str:
        self._next_result += 1
        return f"result{self._next_result}"

    async def split_coin(self, *, coin: object, amounts: list[int]) -> str:
        """Record the split and return a distinct sentinel result."""
        result = self._result()
        self.calls.append(
            ("split_coin", {"coin": coin, "amounts": amounts, "result": result})
        )
        return result

    async def move_call(
        self, *, target: str, arguments: list[object], type_arguments: list[object]
    ) -> str:
        """Record the call and return a distinct sentinel result."""
        result = self._result()
        self.calls.append(
            (
                "move_call",
                {
                    "target": target,
                    "arguments": arguments,
                    "type_arguments": type_arguments,
                    "result": result,
                },
            )
        )
        return result

    async def transfer_objects(self, *, transfers: list[object], recipient: str) -> None:
        """Record the transfer; no return value, matching the real signature."""
        self.calls.append(
            ("transfer_objects", {"transfers": transfers, "recipient": recipient})
        )

    async def build_and_sign(self) -> dict[str, bytes]:
        """Return a fixed txdict; nothing in these tests reads it."""
        return {"tx_bytestr": b"fake"}


class _FakeNetwork:
    def __init__(self) -> None:
        self.system_object = "0xsystem"
        self.wal_coin_type = "0x2::wal::WAL"


class _FakeTuskConfig:
    def __init__(self) -> None:
        self.active_network = "testnet"
        self.network = _FakeNetwork()

    def relay_url_for(self, *, network_name: str, relay_name: str | None) -> str:
        """Return a fixed relay URL, ignoring which relay was asked for."""
        return "https://relay.example"


class _FakePysuiConfig:
    """Controllable active address + sponsor-signability set."""

    def __init__(self, *, active_address: str, signable: frozenset[str]) -> None:
        self.active_address = active_address
        self._signable = signable

    def keypair_for_address(self, *, address: str) -> object:
        """Mirror pysui: raise ValueError when the address has no local key."""
        if address not in self._signable:
            raise ValueError(f"Keypair for address: {address} does not exist.")
        return object()

    def alias_for_address(self, *, address: str) -> str:
        """No-op address validation -- every 0x-address is accepted."""
        return address


class _FakeParityClient:
    """``WalrusClient``-shaped async context manager for parity/guard tests.

    Used as BOTH the CLI simulate path's ``WalrusClient`` and the library
    pipeline's ``client`` argument -- both read the same
    ``config``/``pysui_client`` shape, so one fake class serves both call
    sites.
    """

    def __init__(
        self,
        *,
        active_address: str,
        signable: frozenset[str],
        get_object_response: object | None = None,
    ) -> None:
        self.config = _FakeTuskConfig()
        self.pysui_client = types.SimpleNamespace(
            config=_FakePysuiConfig(active_address=active_address, signable=signable)
        )
        self._get_object_response = get_object_response
        self.txns: list[_RecordingComposeTxn] = []

    async def __aenter__(self) -> "_FakeParityClient":
        """Enter the fake client's async context, returning itself."""
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit the fake client's async context; nothing to clean up."""
        return

    async def committee(self) -> types.SimpleNamespace:
        """Return a minimal object exposing only ``n_shards``."""
        return types.SimpleNamespace(n_shards=1000)

    async def transaction(self, **kwargs: Any) -> _RecordingComposeTxn:
        """Open (and record) a fake composable transaction."""
        txn = _RecordingComposeTxn(
            sender=kwargs.get("initial_sender"), sponsor=kwargs.get("initial_sponsor")
        )
        self.txns.append(txn)
        return txn

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Return the canned GetObject response for a tip-coin usability check."""
        return SuiRpcResult(True, "", self._get_object_response)


def _base_relay_args(**overrides: object) -> argparse.Namespace:
    """Build a complete argparse.Namespace for store_blob_relay, with
    defaults for every attribute the handler and its helpers read."""
    defaults: dict[str, object] = {
        "content": "hello world",
        "file": None,
        "epochs": 5,
        "permanent": False,
        "max_tip": None,
        "relay": None,
        "tip_source": FROM_GAS,
        "recipient": None,
        "full_json": False,
        "sender": None,
        "sponsor": None,
        "mode": "simulate",
        "from_cfg_path": None,
        "active_network": None,
        "pysui_config_path": None,
        "pysui_group_name": None,
        "pysui_profile_name": None,
        "pysui_address": None,
        "pysui_alias": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _fixed_encoded_blob() -> EncodedBlob:
    """A deterministic EncodedBlob shared by both paths under test."""
    return EncodedBlob(
        blob_id=b"\x01" * 32,
        root_hash=b"\x02" * 32,
        unencoded_length=11,
        n_shards=1000,
        slivers=(),
        metadata_bcs=b"",
    )


def _fixed_auth_package() -> AuthPackage:
    """A deterministic AuthPackage shared by both paths under test.

    ``build_auth_package`` generates a FRESH random nonce on every real
    call -- both paths must be patched to return this SAME object so their
    composed PTBs carry byte-identical input 0.
    """
    return AuthPackage(
        nonce=b"\x03" * 32,
        blob_digest=b"\x04" * 32,
        nonce_digest=b"\x05" * 32,
        unencoded_length=11,
    )


def _fixed_quote() -> TipQuote:
    """A deterministic tip-required quote shared by both paths under test."""
    return TipQuote(address="0xrelay", amount=1000, kind=ConstTip(amount=1000))


class _StopAfterCompose(Exception):
    """Raised by the patched ``execute_registration_txn`` to bail the
    library pipeline out immediately after Tx1 is composed -- so this test
    never has to fake the relay POST/certify stages that follow."""


class TestRelaySimulateMatchesPipelineComposition:
    """CLI ``store_blob_relay --mode simulate`` composes the SAME Tx1 as
    the library's ``store_blob_relay`` execute path, for identical inputs.
    See this module's docstring for why a real ``AsyncSuiTransaction`` was
    not used, and why the weaker "same kwargs to add_registration_sequence"
    fallback was not needed."""

    async def test_composed_ptb_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        encoded = _fixed_encoded_blob()
        quote = _fixed_quote()
        auth_package = _fixed_auth_package()

        def _fake_encode_blob(*, data: bytes, n_shards: int) -> EncodedBlob:
            return encoded

        async def _fake_quote_tip(**kwargs: Any) -> TipQuote:
            return quote

        def _fake_build_auth_package(*, data: bytes) -> AuthPackage:
            return auth_package

        async def _fake_resolve_package_id(*, client: object, system_object: str) -> str:
            return "0xpkg"

        async def _fake_select_wal_payment_coin(*, client: object, owner: str) -> str:
            return "0xwal"

        monkeypatch.setattr(
            blob_execute_module, "select_wal_payment_coin", _fake_select_wal_payment_coin
        )

        # -- Library pipeline (execute) path: compose Tx1 for real, then
        # bail out via _StopAfterCompose before execute_registration_txn
        # would sign/submit/read anything back.
        captured: dict[str, _RecordingComposeTxn] = {}

        async def _stop_after_compose(
            *, client: object, txn: _RecordingComposeTxn, **kwargs: Any
        ) -> None:
            captured["pipeline_txn"] = txn
            raise _StopAfterCompose()

        monkeypatch.setattr(pipeline_module, "encode_blob", _fake_encode_blob)
        monkeypatch.setattr(pipeline_module, "quote_tip", _fake_quote_tip)
        monkeypatch.setattr(pipeline_module, "build_auth_package", _fake_build_auth_package)
        monkeypatch.setattr(pipeline_module, "resolve_package_id", _fake_resolve_package_id)
        monkeypatch.setattr(
            registration_module, "execute_registration_txn", _stop_after_compose
        )

        pipeline_client = _FakeParityClient(active_address="0xsender", signable=frozenset())
        with pytest.raises(_StopAfterCompose):
            await pipeline_module.store_blob_relay(
                client=pipeline_client,
                data=b"hello world",
                epochs=5,
                deletable=True,
                relay_name=None,
                sender=None,
                sponsor=None,
                recipient=None,
                tip_source=FROM_GAS,
                wal_payment_coin=None,
            )
        pipeline_txn = captured["pipeline_txn"]

        # -- CLI simulate path: same inputs, same real
        # add_registration_sequence/preflight_sponsor/preflight_payment.
        monkeypatch.setattr(tusky_cmds_relay, "config_from_args", lambda args: object())
        cli_client = _FakeParityClient(active_address="0xsender", signable=frozenset())
        monkeypatch.setattr(
            tusky_cmds_relay, "WalrusClient", lambda *, pytusk_config: cli_client
        )
        monkeypatch.setattr(tusky_cmds_relay, "encode_blob", _fake_encode_blob)
        monkeypatch.setattr(tusky_cmds_relay, "quote_tip", _fake_quote_tip)
        monkeypatch.setattr(tusky_cmds_relay, "build_auth_package", _fake_build_auth_package)

        async def _fake_walrus_package_id(*, client: object) -> tuple[str, str]:
            return "0xsystem", "0xpkg"

        monkeypatch.setattr(tusky_cmds_relay, "walrus_package_id", _fake_walrus_package_id)

        async def _fake_submit(*, client: object, txdict: dict, mode: str) -> SuiRpcResult:
            return SuiRpcResult(True, "", types.SimpleNamespace())

        monkeypatch.setattr(tusky_cmds_relay, "submit", _fake_submit)

        async def _fake_cost(
            *, client: object, transaction: object
        ) -> tuple[dict, dict]:
            return {}, {}

        monkeypatch.setattr(
            tusky_cmds_relay, "simulate_cost_from_balance_changes", _fake_cost
        )

        args = _base_relay_args(
            epochs=5, permanent=False, tip_source=FROM_GAS, content="hello world"
        )
        await tusky_cmds_relay.store_blob_relay(args)

        assert len(cli_client.txns) == 1
        cli_txn = cli_client.txns[0]

        # The actual assertion: both real compose functions produced the
        # SAME sequence of PTB inputs (real ProgrammableTransactionBuilder,
        # so this is the real BCS pure-input list) and the same ordered
        # command log, from the same inputs.
        cli_inputs = list(cli_txn.builder.inputs.values())
        pipeline_inputs = list(pipeline_txn.builder.inputs.values())
        assert len(cli_inputs) == len(pipeline_inputs) == 1
        assert cli_inputs[0].enum_name == pipeline_inputs[0].enum_name == "Pure"
        assert cli_inputs[0].value == pipeline_inputs[0].value
        assert len(cli_inputs[0].value) == 72  # the auth package, byte-exact

        assert cli_txn.calls == pipeline_txn.calls
        kinds = [kind for kind, _ in cli_txn.calls]
        assert kinds == [
            "split_coin",
            "transfer_objects",
            "move_call",
            "move_call",
            "transfer_objects",
        ]


class TestRelaySimulateGuardsNowRun:
    """The two guards the CLI simulate path used to skip now run for real:
    an unusable tip coin and an unsignable sponsor both fail simulate
    instead of silently producing a result a real run could never reach."""

    async def test_raises_for_unusable_tip_coin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A --tip-source coin owned by neither sender nor sponsor must
        fail simulate via preflight_payment's assert_coin_usable check,
        exactly as it would fail execute."""
        encoded = _fixed_encoded_blob()
        quote = _fixed_quote()
        auth_package = _fixed_auth_package()

        monkeypatch.setattr(tusky_cmds_relay, "config_from_args", lambda args: object())
        bad_coin = types.SimpleNamespace(
            owner=types.SimpleNamespace(address="0xsomeoneelse"), balance=999_999
        )
        client = _FakeParityClient(
            active_address="0xsender", signable=frozenset(), get_object_response=bad_coin
        )
        monkeypatch.setattr(tusky_cmds_relay, "WalrusClient", lambda *, pytusk_config: client)
        monkeypatch.setattr(
            tusky_cmds_relay, "encode_blob", lambda *, data, n_shards: encoded
        )

        async def _fake_quote_tip(**kwargs: Any) -> TipQuote:
            return quote

        monkeypatch.setattr(tusky_cmds_relay, "quote_tip", _fake_quote_tip)
        monkeypatch.setattr(
            tusky_cmds_relay, "build_auth_package", lambda *, data: auth_package
        )

        async def _fake_walrus_package_id(*, client: object) -> tuple[str, str]:
            return "0xsystem", "0xpkg"

        monkeypatch.setattr(tusky_cmds_relay, "walrus_package_id", _fake_walrus_package_id)

        args = _base_relay_args(tip_source="0xbadcoin", content="hello world")

        with pytest.raises(SystemExit) as excinfo:
            await tusky_cmds_relay.store_blob_relay(args)

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Error in payment preflight" in captured.err
        assert "is owned by" in captured.err

    async def test_raises_for_unsignable_sponsor(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A --sponsor with no local keypair must fail simulate via
        preflight_sponsor, before any encode or network work -- matching
        the library pipeline's fail-fast ordering."""
        monkeypatch.setattr(tusky_cmds_relay, "config_from_args", lambda args: object())
        client = _FakeParityClient(active_address="0xsender", signable=frozenset())
        monkeypatch.setattr(tusky_cmds_relay, "WalrusClient", lambda *, pytusk_config: client)

        args = _base_relay_args(sponsor="0xstranger", content="hello world")

        with pytest.raises(SystemExit) as excinfo:
            await tusky_cmds_relay.store_blob_relay(args)

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "not signable" in captured.err
        # Nothing was ever opened -- the guard ran before any transaction.
        assert client.txns == []
