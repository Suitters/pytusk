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

from pytusk.commands.walrus_command import (
    WalrusCommand,
    http_failure_message,
)
from pytusk.core.types import (
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


def _tip_amount(*, value: object, field: str) -> int:
    """Return ``value`` as a validated MIST tip amount.

    ``bool`` is rejected EXPLICITLY rather than by omission: it is a
    subclass of ``int``, so a JSON ``true`` would otherwise be accepted and
    silently become a tip of 1 MIST. Negative values are rejected because
    the amount is a ``u64`` on the wire -- a negative one means the relay
    sent something pytusk does not model, not a discount.

    Neither :class:`ConstTip` nor :class:`LinearTip` validates its own
    fields, so this is the only gate between the relay's JSON and a tip
    amount the caller is asked to pay.

    Args:
        value (object): The decoded JSON value.
        field (str): Field name, used to build the error message.

    Returns:
        int: The validated amount, in MIST.

    Raises:
        ValueError: If ``value`` is not a non-negative, non-boolean integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{field} must not be negative, got {value}")
    return value


def _parse_tip_kind(*, payload: object) -> TipKind:
    """Parse Walrus's externally tagged ``TipKind`` JSON.

    Every amount goes through :func:`_tip_amount`. This runs while parsing
    ``GET /v1/tip-config``, which is PRE-SPEND, so raising here is correct
    under the error boundary: nothing has been paid yet.

    Args:
        payload (object): The decoded ``kind`` value.

    Returns:
        TipKind: The parsed tip formula.

    Raises:
        ValueError: If the payload does not match either variant, or if any
            amount is not a non-negative, non-boolean integer.
    """
    if not isinstance(payload, dict) or len(payload) != 1:
        raise ValueError(f"Unrecognised tip kind payload: {payload!r}")
    tag, body = next(iter(payload.items()))
    if tag == "const":
        return ConstTip(amount=_tip_amount(value=body, field="const tip amount"))
    if tag == "linear":
        if not isinstance(body, dict):
            raise ValueError(f"linear tip body must be an object, got {body!r}")
        try:
            base = body["base"]
            multiplier = body["encoded_size_mul_per_kib"]
        except KeyError as exc:
            raise ValueError(f"linear tip is missing field {exc}") from exc
        return LinearTip(
            base=_tip_amount(value=base, field="linear tip base"),
            encoded_size_mul_per_kib=_tip_amount(
                value=multiplier, field="linear tip encoded_size_mul_per_kib"
            ),
        )
    raise ValueError(f"Unknown tip kind {tag!r}")


def _tip_address(*, value: object) -> str:
    """Return ``value`` as a validated Sui address for the tip transfer.

    The relay names its own payee, so a malformed address costs the caller
    nothing directly. It is refused HERE so the failure reports as an
    unusable tip config, pre-spend, rather than surfacing later as an
    opaque error from inside PTB composition. This is the address side of
    the gate :func:`_tip_amount` provides for the amount.

    Shortened forms are accepted: a Sui address is at most 32 bytes and
    leading zeroes may be omitted, so the hex is left-padded before it is
    checked rather than requiring the full 64 digits.

    Args:
        value (object): The decoded ``address`` value.

    Returns:
        str: The validated address, unchanged.

    Raises:
        ValueError: If ``value`` is not a well-formed Sui address.
    """
    if not isinstance(value, str):
        raise ValueError(f"tip address must be a string, got {value!r}")
    if not value.startswith("0x"):
        raise ValueError(f"tip address must be 0x-prefixed, got {value!r}")
    digits = value[2:]
    if not digits or len(digits) > 64:
        raise ValueError(f"tip address is not a Sui address: {value!r}")
    try:
        bytes.fromhex(digits.rjust(64, "0"))
    except ValueError as exc:
        raise ValueError(f"tip address is not valid hex: {value!r}") from exc
    return value


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
        return TipConfig(
            address=_tip_address(value=address),
            kind=_parse_tip_kind(payload=kind),
        )
    raise ValueError(f"Unrecognised tip config payload: {payload!r}")


@dataclasses.dataclass(kw_only=True)
class ReadTipConfig(WalrusCommand):
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
            return SuiRpcResult(
                False,
                http_failure_message(response=response, context="tip_config"),
            )
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
                http_failure_message(
                    response=response, context=f"blob_id={self.blob_id}"
                ),
                # The ack deliberately keeps `relay_message=response.text` --
                # the RAW body, not the formatted diagnostic. The pipeline
                # surfaces this field on a RelayBlobReceipt for someone
                # resuming an upload, where what the relay ACTUALLY said is
                # the useful thing; the formatted message goes to
                # result_string for the log.
                RelayUploadAck(
                    outcome=RelayUploadOutcome.REFUSED,
                    blob_id=None,
                    confirmation_certificate=None,
                    relay_status=response.status_code,
                    relay_message=response.text,
                ),
            )
        try:
            data = response.json()
        except ValueError as exc:
            # A 2xx whose body is not JSON at all -- an HTML error page from
            # a proxy or CDN, a redirect body -- must NOT escape as an
            # exception. This runs POST-SPEND: the tip is paid and the blob
            # is registered, so raising here would destroy the nonce the
            # caller needs to resume. Reported as REFUSED, matching the
            # malformed-but-decodable 2xx case below.
            return SuiRpcResult(
                False,
                f"Relay returned a non-JSON body (HTTP {response.status_code}): {exc}",
                RelayUploadAck(
                    outcome=RelayUploadOutcome.REFUSED,
                    blob_id=None,
                    confirmation_certificate=None,
                    relay_status=response.status_code,
                    relay_message=response.text,
                ),
            )
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
