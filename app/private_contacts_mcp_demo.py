from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from app.mcp import DEFAULT_MCP_PROTOCOL_VERSION, PRIVATE_VALUES_META_KEY

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_CONTACT_LIMIT = 10
MAX_CONTACT_LIMIT = 50
MAX_CARDDAV_RESPONSE_BYTES = 5_000_000
CARDDAV_URL_ENV = "MINIGENT_CARDDAV_URL"
CARDDAV_USERNAME_ENV = "MINIGENT_CARDDAV_USERNAME"
CARDDAV_PASSWORD_ENV = "MINIGENT_CARDDAV_PASSWORD"
CARDDAV_NAMESPACE = "urn:ietf:params:xml:ns:carddav"
DAV_NAMESPACE = "DAV:"
CARDDAV_REPORT_BODY = b"""<?xml version="1.0" encoding="utf-8" ?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:getetag />
    <card:address-data />
  </d:prop>
</card:addressbook-query>
"""
CARDDAV_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop><d:resourcetype /></d:prop>
</d:propfind>
"""

CONTACTS_LIST_TOOL = {
    "name": "contacts_list",
    "description": (
        "List contacts. Contact fields are opaque {{pii:kind:reference}} placeholders; "
        "preserve each placeholder exactly when answering the user."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_CONTACT_LIMIT,
                "description": f"Maximum contacts to return. Defaults to {DEFAULT_CONTACT_LIMIT}.",
            }
        },
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "contacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "emails": {"type": "array", "items": {"type": "string"}},
                        "phones": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "emails", "phones"],
                    "additionalProperties": False,
                },
            },
            "truncated": {"type": "boolean"},
        },
        "required": ["contacts", "truncated"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class Contact:
    name: str
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()


DEMO_CONTACTS = (
    Contact(name="Alice Smith", emails=("alice@example.com",), phones=("+1 555 0100",)),
    Contact(name="Bob Jones", emails=("bob@example.com",), phones=("+1 555 0101",)),
)


class ContactSource(Protocol):
    def list_contacts(self, *, limit: int) -> tuple[list[Contact], bool]: ...


class StaticContactSource:
    def __init__(self, contacts: Sequence[Contact] = DEMO_CONTACTS) -> None:
        self._contacts = tuple(contacts)

    def list_contacts(self, *, limit: int) -> tuple[list[Contact], bool]:
        return list(self._contacts[:limit]), len(self._contacts) > limit


class CardDAVContactSource:
    def __init__(
        self,
        *,
        addressbook_url: str,
        username: str,
        password: str,
        verify_tls: bool = True,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not addressbook_url.strip():
            raise ValueError("CardDAV address-book URL is required")
        parsed_addressbook_url = urlsplit(addressbook_url)
        if (
            parsed_addressbook_url.scheme not in {"http", "https"}
            or not parsed_addressbook_url.hostname
        ):
            raise ValueError("CardDAV address-book URL must use HTTP or HTTPS")
        if parsed_addressbook_url.username or parsed_addressbook_url.password:
            raise ValueError("CardDAV credentials must not be embedded in the address-book URL")
        if not username:
            raise ValueError("CardDAV username is required")
        if not password:
            raise ValueError("CardDAV password is required")
        self._addressbook_url = addressbook_url
        self._username = username
        self._password = password
        self._verify_tls = verify_tls
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def list_contacts(self, *, limit: int) -> tuple[list[Contact], bool]:
        try:
            with httpx.Client(
                auth=httpx.BasicAuth(self._username, self._password),
                verify=self._verify_tls,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                discovery_response = client.request(
                    "PROPFIND",
                    self._addressbook_url,
                    headers={
                        "Depth": "1",
                        "Content-Type": "application/xml; charset=utf-8",
                        "Accept": "application/xml",
                    },
                    content=CARDDAV_PROPFIND_BODY,
                )
                discovery_response.raise_for_status()
                if len(discovery_response.content) > MAX_CARDDAV_RESPONSE_BYTES:
                    raise RuntimeError("CardDAV response exceeded the private contacts size limit")
                addressbook_url = discover_carddav_addressbook_url(
                    discovery_response.content,
                    base_url=self._addressbook_url,
                )
                response = client.request(
                    "REPORT",
                    addressbook_url,
                    headers={
                        "Depth": "1",
                        "Content-Type": "application/xml; charset=utf-8",
                        "Accept": "application/xml",
                    },
                    content=CARDDAV_REPORT_BODY,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"CardDAV address-book query failed with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("CardDAV address-book query failed") from exc

        if len(response.content) > MAX_CARDDAV_RESPONSE_BYTES:
            raise RuntimeError("CardDAV response exceeded the private contacts size limit")
        contacts = parse_carddav_multistatus(response.content)
        return contacts[:limit], len(contacts) > limit


class PrivateContactsMCPServer:
    def __init__(
        self,
        contacts: Sequence[Contact] | None = None,
        *,
        contact_source: ContactSource | None = None,
        reference_factory: Callable[[], str] | None = None,
    ) -> None:
        if contacts is not None and contact_source is not None:
            raise ValueError("Provide contacts or contact_source, not both")
        static_contacts = DEMO_CONTACTS if contacts is None else contacts
        self._contact_source = contact_source or StaticContactSource(static_contacts)
        self._reference_factory = reference_factory or (lambda: secrets.token_urlsafe(16))

    def handle(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        request_id = payload.get("id")
        method = payload.get("method")
        if method == "notifications/initialized":
            return None
        if request_id is None:
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": DEFAULT_MCP_PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": "minigent-private-contacts",
                        "version": "0.1.0",
                    },
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            return self._result(request_id, {"tools": [CONTACTS_LIST_TOOL]})
        if method == "tools/call":
            try:
                return self._handle_tool_call(request_id, payload.get("params"))
            except Exception as exc:
                return self._error(request_id, -32000, str(exc))
        return self._error(request_id, -32601, f"Unsupported MCP method '{method}'")

    def _handle_tool_call(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "tools/call params must be an object")
        if params.get("name") != "contacts_list":
            return self._error(request_id, -32602, "Unknown tool")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "tool arguments must be an object")
        unknown_arguments = set(arguments) - {"limit"}
        if unknown_arguments:
            return self._error(request_id, -32602, "contacts_list received unknown arguments")
        limit = arguments.get("limit", DEFAULT_CONTACT_LIMIT)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_CONTACT_LIMIT
        ):
            return self._error(
                request_id,
                -32602,
                f"limit must be an integer from 1 to {MAX_CONTACT_LIMIT}",
            )

        contacts, truncated = self._contact_source.list_contacts(limit=limit)
        structured_content: dict[str, Any] = {"contacts": [], "truncated": truncated}
        private_values: dict[str, str] = {}
        for contact in contacts:
            structured_content["contacts"].append(
                {
                    "name": self._protect("name", contact.name, private_values),
                    "emails": [
                        self._protect("email", email, private_values) for email in contact.emails
                    ],
                    "phones": [
                        self._protect("phone", phone, private_values) for phone in contact.phones
                    ],
                }
            )

        return self._result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Found {len(contacts)} contacts. Preserve the placeholders from "
                            "structuredContent exactly in the final answer."
                        ),
                    }
                ],
                "structuredContent": structured_content,
                "_meta": {PRIVATE_VALUES_META_KEY: private_values},
            },
        )

    def _protect(self, kind: str, value: str, private_values: dict[str, str]) -> str:
        reference = self._reference_factory()
        private_values[reference] = value
        return f"{{{{pii:{kind}:{reference}}}}}"

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def discover_carddav_addressbook_url(payload: bytes, *, base_url: str) -> str:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("CardDAV server returned invalid XML") from exc
    response_tag = f"{{{DAV_NAMESPACE}}}response"
    href_tag = f"{{{DAV_NAMESPACE}}}href"
    addressbook_tag = f"{{{CARDDAV_NAMESPACE}}}addressbook"
    for response in root.iter(response_tag):
        if next(response.iter(addressbook_tag), None) is None:
            continue
        href = response.find(href_tag)
        if href is not None and href.text:
            discovered_url = urljoin(base_url, href.text.strip())
            if _url_origin(discovered_url) != _url_origin(base_url):
                raise RuntimeError("CardDAV discovery returned a cross-origin address book")
            return discovered_url
    return base_url


def _url_origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(value)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), parsed.hostname, port


def parse_carddav_multistatus(payload: bytes) -> list[Contact]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("CardDAV server returned invalid XML") from exc
    contacts: list[Contact] = []
    address_data_tag = f"{{{CARDDAV_NAMESPACE}}}address-data"
    for address_data in root.iter(address_data_tag):
        if address_data.text:
            contact = parse_vcard(address_data.text)
            if contact is not None:
                contacts.append(contact)
    return contacts


def parse_vcard(payload: str) -> Contact | None:
    fields: dict[str, list[str]] = {}
    for line in _unfold_vcard_lines(payload):
        left, separator, raw_value = line.partition(":")
        if not separator:
            continue
        property_name = left.split(";", 1)[0].rsplit(".", 1)[-1].upper()
        if property_name not in {"FN", "N", "EMAIL", "TEL"}:
            continue
        fields.setdefault(property_name, []).append(_unescape_vcard_value(raw_value))

    name = next((value.strip() for value in fields.get("FN", []) if value.strip()), "")
    if not name:
        name = _name_from_structured_value(fields.get("N", []))
    emails = _unique_nonempty(fields.get("EMAIL", []))
    phones = _unique_nonempty(fields.get("TEL", []))
    if not name and not emails and not phones:
        return None
    return Contact(name=name or "Unnamed contact", emails=emails, phones=phones)


def _unfold_vcard_lines(payload: str) -> list[str]:
    unfolded: list[str] = []
    for line in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _unescape_vcard_value(value: str) -> str:
    return re.sub(
        r"\\([nN,;\\])",
        lambda match: "\n" if match.group(1).lower() == "n" else match.group(1),
        value,
    )


def _name_from_structured_value(values: Sequence[str]) -> str:
    for value in values:
        parts = value.split(";")
        family = parts[0].strip() if parts else ""
        given = parts[1].strip() if len(parts) > 1 else ""
        name = " ".join(part for part in (given, family) if part)
        if name:
            return name
    return ""


def _unique_nonempty(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def create_app(server: PrivateContactsMCPServer | None = None) -> FastAPI:
    app = FastAPI(title="Minigent private contacts MCP")
    private_contacts = server or PrivateContactsMCPServer()

    @app.post("/mcp", response_model=None)
    async def mcp_endpoint(request: Request) -> Response | dict[str, Any]:
        try:
            payload = await request.json()
        except ValueError:
            return _invalid_request_response(-32700, "Invalid JSON")
        if not isinstance(payload, dict):
            return _invalid_request_response(-32600, "Payload must be an object")
        result = await asyncio.to_thread(private_contacts.handle, payload)
        if result is None:
            return Response(status_code=202)
        return result

    return app


def _invalid_request_response(code: int, message: str) -> Response:
    return Response(
        content=json.dumps(
            {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
        ),
        status_code=400,
        media_type="application/json",
    )


app = create_app()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a local MCP server with private contact placeholders."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="Disable CardDAV TLS verification. Use only for trusted local development.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    addressbook_url = os.environ.get(CARDDAV_URL_ENV, "").strip()
    if addressbook_url:
        contact_source: ContactSource = CardDAVContactSource(
            addressbook_url=addressbook_url,
            username=os.environ.get(CARDDAV_USERNAME_ENV, ""),
            password=os.environ.get(CARDDAV_PASSWORD_ENV, ""),
            verify_tls=not args.insecure_skip_tls_verify,
        )
        server = PrivateContactsMCPServer(contact_source=contact_source)
    else:
        server = PrivateContactsMCPServer()
    uvicorn.run(create_app(server), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
