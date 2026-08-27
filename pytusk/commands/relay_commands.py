#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus upload relay HTTP commands.

Both commands carry ``endpoint_role = "relay"``, which -- like
``storage_node`` and unlike ``aggregator``/``publisher`` -- REQUIRES an
explicit ``base_url`` at execute time. A relay is chosen by name from a
list, so there is no single configured URL to fall back on; the caller
resolves one via :meth:`~pytusk.config.tusk_config.PytuskConfiguration.relay_url_for`
and passes it in.
"""

import dataclasses
from typing import Any, ClassVar

import httpx
from pysui import SuiRpcResult

from pytusk.commands.walrus_command import WalrusCommand
from pytusk.core.relay_types import (
    ConstTip,
    LinearTip,
    RelayUploadOutcome,
    TipConfig,
    TipKind,
)


@dataclasses.dataclass(kw_only=True, frozen=True)
class RelayUploadAck:
    """A relay's answer to an upload -- whatever that answer was.

    ALWAYS the type in ``SuiRpcResult.result_data`` for this command, on
    both the success and the failure path. A caller therefore reads
    :attr:`outcome` to learn what happened, and never has to check
    ``is_ok()`` first to know which type it is holding.

    :attr:`RelayUploadOutcome.UNANSWERED` can never appear here: this
    object exists only because an HTTP response arrived. Distinguishing "no
    answer" from "an answer" is the transport layer's job, and belongs to
    :func:`~pytusk.core.relay_upload.upload.upload_to_relay`.

    Attributes:
        outcome (RelayUploadOutcome): ``UPLOADED`` or ``REFUSED``.
        blob_id (str | None): Blob ID the relay confirms it stored; set
            when ``UPLOADED``.
        confirmation_certificate (dict | None): The certificate, still as
            raw JSON, set when ``UPLOADED``. Its three parts are encoded
            inconsistently, so parsing is left to
            :func:`~pytusk.core.relay_upload.relay_certify.parse_relay_certificate`
            rather than done here.
        relay_status (int | None): HTTP status the relay answered with.
        relay_message (str | None): Response body, verbatim, when the relay
            refused. Only the body distinguishes which 400 occurred.
    """

    outcome: RelayUploadOutcome
    blob_id: str | None
    confirmation_certificate: dict | None
    relay_status: int | None
    relay_message: str | None


def _parse_tip_kind(*, payload: object) -> TipKind:
    """Parse Walrus's externally tagged ``TipKind`` JSON.

    Args:
        payload (object): The decoded ``kind`` value.

    Returns:
        TipKind: The parsed tip formula.

    Raises:
        ValueError: If the payload does not match either variant.
    """
    if not isinstance(payload, dict) or len(payload) != 1:
        raise ValueError(f"Unrecognised tip kind payload: {payload!r}")
    tag, body = next(iter(payload.items()))
    if tag == "const":
        if not isinstance(body, int):
            raise ValueError(f"const tip amount must be an integer, got {body!r}")
        return ConstTip(amount=body)
    if tag == "linear":
        if not isinstance(body, dict):
            raise ValueError(f"linear tip body must be an object, got {body!r}")
        try:
            return LinearTip(
                base=body["base"],
                encoded_size_mul_per_kib=body["encoded_size_mul_per_kib"],
            )
        except KeyError as exc:
            raise ValueError(f"linear tip is missing field {exc}") from exc
    raise ValueError(f"Unknown tip kind {tag!r}")


def _parse_tip_config(*, payload: object) -> TipConfig:
    """Parse Walrus's externally tagged ``TipConfig`` JSON.

    A relay that charges nothing serialises as the BARE STRING ``"no_tip"``,
    not an object -- both Walrus enums are serde externally tagged with
    ``rename_all = "snake_case"``.

    Args:
        payload (object): The decoded response body.

    Returns:
        TipConfig: The relay's tipping policy.

    Raises:
        ValueError: If the payload matches neither variant.
    """
    if payload == "no_tip":
        return TipConfig(address=None, kind=None)
    if isinstance(payload, dict) and set(payload) == {"send_tip"}:
        body = payload["send_tip"]
        if not isinstance(body, dict):
            raise ValueError(f"send_tip body must be an object, got {body!r}")
        try:
            address = body["address"]
            kind = body["kind"]
        except KeyError as exc:
            raise ValueError(f"send_tip is missing field {exc}") from exc
        return TipConfig(address=address, kind=_parse_tip_kind(payload=kind))
    raise ValueError(f"Unrecognised tip config payload: {payload!r}")


@dataclasses.dataclass(kw_only=True)
class GetTipConfig(WalrusCommand):
    """Fetch a relay's tipping policy.

    GET {relay}/v1/tip-config

    ``base_url`` is a specific relay's URL -- see :attr:`endpoint_role`.
    """

    endpoint_role: ClassVar[str] = "relay"

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/tip-config"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, f"HTTP {response.status_code}: {response.text}")
        try:
            return SuiRpcResult(True, "", _parse_tip_config(payload=response.json()))
        except ValueError as exc:
            return SuiRpcResult(False, str(exc))


@dataclasses.dataclass(kw_only=True)
class UploadRelayBlob(WalrusCommand):
    """Hand an unencoded blob to a relay, which fans slivers out for us.

    POST {relay}/v1/blob-upload-relay

    ``base_url`` is a specific relay's URL -- see :attr:`endpoint_role`.

    The blob must already be REGISTERED on chain, and when the relay
    charges a tip that tip must already be executed and confirmed -- the
    relay fetches the transaction itself and rejects on an absent
    timestamp. Posting early yields 401, not a retryable error.

    Args:
        blob_id (str): Blob ID as URL-safe unpadded base64.
        data (bytes): The raw UNENCODED blob bytes. The relay performs the
            encoding; the server caps the body at 1 GiB.
        register_tip_tx_digest (str | None): Digest of the transaction that
            paid the tip, sent as ``tx_id``. Omit only against a
            ``no_tip`` relay.
        nonce (str | None): Base64url nonce from the authentication
            package. Omit only against a ``no_tip`` relay.
        deletable_blob_object (str | None): Object ID when the blob was
            registered deletable. OMITTED means permanent -- the two are
            distinguished by presence, not by a boolean.
        encoding_type (int | None): Omitted means the relay's default, RS2,
            which is the only live variant.
    """

    endpoint_role: ClassVar[str] = "relay"

    blob_id: str
    data: bytes
    register_tip_tx_digest: str | None = None
    nonce: str | None = None
    deletable_blob_object: str | None = None
    encoding_type: int | None = None

    def http_method(self) -> str:
        return "POST"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blob-upload-relay"

    def query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"blob_id": self.blob_id}
        if self.register_tip_tx_digest is not None:
            params["tx_id"] = self.register_tip_tx_digest
        if self.nonce is not None:
            params["nonce"] = self.nonce
        if self.deletable_blob_object is not None:
            params["deletable_blob_object"] = self.deletable_blob_object
        if self.encoding_type is not None:
            params["encoding_type"] = self.encoding_type
        return params

    def request_body(self) -> bytes | None:
        return self.data

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(
                False,
                f"HTTP {response.status_code}: {response.text}",
                RelayUploadAck(
                    outcome=RelayUploadOutcome.REFUSED,
                    blob_id=None,
                    confirmation_certificate=None,
                    relay_status=response.status_code,
                    relay_message=response.text,
                ),
            )
        data = response.json()
        try:
            ack = RelayUploadAck(
                outcome=RelayUploadOutcome.UPLOADED,
                blob_id=data["blob_id"],
                confirmation_certificate=data["confirmation_certificate"],
                relay_status=response.status_code,
                relay_message=None,
            )
        except (KeyError, TypeError) as exc:
            # A 2xx whose body is not the documented shape is definitive,
            # not transient: retrying returns the same malformed body. It
            # is reported as REFUSED so the retry loop stops, with the body
            # preserved verbatim for diagnosis.
            return SuiRpcResult(
                False,
                f"Unexpected relay response: {data!r} ({exc})",
                RelayUploadAck(
                    outcome=RelayUploadOutcome.REFUSED,
                    blob_id=None,
                    confirmation_certificate=None,
                    relay_status=response.status_code,
                    relay_message=response.text,
                ),
            )
        return SuiRpcResult(True, "", ack)
