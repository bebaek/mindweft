from __future__ import annotations

import base64

import httpx
import pytest

from app.mcp import PRIVATE_VALUES_META_KEY
from app.private_contacts_mcp_demo import (
    CardDAVContactSource,
    Contact,
    PrivateContactsMCPServer,
    discover_carddav_addressbook_url,
    parse_carddav_multistatus,
    parse_vcard,
)


def test_parse_vcard_reads_unfolded_name_emails_and_phones() -> None:
    contact = parse_vcard(
        "\r\n".join(
            [
                "BEGIN:VCARD",
                "VERSION:3.0",
                "FN:Alice\\, Example",
                "EMAIL;TYPE=HOME:alice@example.com",
                "item1.EMAIL;TYPE=WORK:alice@work.example",
                "TEL;TYPE=CELL:+1 555 0100",
                "TEL;TYPE=WORK:+1 555",
                " 0101",
                "END:VCARD",
            ]
        )
    )

    assert contact == Contact(
        name="Alice, Example",
        emails=("alice@example.com", "alice@work.example"),
        phones=("+1 555 0100", "+1 5550101"),
    )


def test_parse_vcard_falls_back_to_structured_name() -> None:
    contact = parse_vcard("BEGIN:VCARD\nN:Example;Alice;;;\nEND:VCARD\n")

    assert contact == Contact(name="Alice Example")


def test_discover_carddav_addressbook_url_resolves_relative_href() -> None:
    result = discover_carddav_addressbook_url(
        b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response>
    <d:href>/dav.php/addressbooks/user/default/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection /><card:addressbook />
    </d:resourcetype></d:prop></d:propstat>
  </d:response>
</d:multistatus>""",
        base_url="https://baikal.example/dav.php/addressbooks/user/",
    )

    assert result == "https://baikal.example/dav.php/addressbooks/user/default/"


def test_discover_carddav_addressbook_url_rejects_cross_origin_href() -> None:
    with pytest.raises(RuntimeError, match="cross-origin"):
        discover_carddav_addressbook_url(
            b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response><d:href>https://attacker.example/contacts/</d:href><d:propstat><d:prop>
    <d:resourcetype><card:addressbook /></d:resourcetype>
  </d:prop></d:propstat></d:response>
</d:multistatus>""",
            base_url="https://baikal.example/dav.php/addressbooks/user/",
        )


def test_parse_carddav_multistatus_extracts_address_data() -> None:
    contacts = parse_carddav_multistatus(
        b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response><d:propstat><d:prop><card:address-data><![CDATA[
BEGIN:VCARD
VERSION:3.0
FN:Alice Smith
EMAIL:alice@example.com
TEL:+1 555 0100
END:VCARD
  ]]></card:address-data></d:prop></d:propstat></d:response>
</d:multistatus>"""
    )

    assert contacts == [
        Contact(
            name="Alice Smith",
            emails=("alice@example.com",),
            phones=("+1 555 0100",),
        )
    ]


def test_carddav_source_discovers_collection_and_reports_with_basic_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                headers={"content-type": "application/xml"},
                content=b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response><d:href>/dav.php/addressbooks/user/default/</d:href><d:propstat><d:prop>
    <d:resourcetype><d:collection /><card:addressbook /></d:resourcetype>
  </d:prop></d:propstat></d:response>
</d:multistatus>""",
            )
        assert request.method == "REPORT"
        assert str(request.url) == "https://baikal.example/dav.php/addressbooks/user/default/"
        return httpx.Response(
            207,
            headers={"content-type": "application/xml"},
            content=b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response><d:propstat><d:prop><card:address-data><![CDATA[
BEGIN:VCARD
FN:Alice Smith
EMAIL:alice@example.com
END:VCARD
]]></card:address-data></d:prop></d:propstat></d:response>
  <d:response><d:propstat><d:prop><card:address-data><![CDATA[
BEGIN:VCARD
FN:Bob Jones
EMAIL:bob@example.com
END:VCARD
]]></card:address-data></d:prop></d:propstat></d:response>
</d:multistatus>""",
        )

    source = CardDAVContactSource(
        addressbook_url="https://baikal.example/dav.php/addressbooks/user/",
        username="user",
        password="password",
        auth_mode="basic",
        transport=httpx.MockTransport(handler),
    )

    contacts, truncated = source.list_contacts(limit=1)

    assert contacts == [Contact(name="Alice Smith", emails=("alice@example.com",))]
    assert truncated is True
    assert len(requests) == 2
    discovery_request, report_request = requests
    assert discovery_request.method == "PROPFIND"
    assert discovery_request.headers["depth"] == "1"
    assert b"propfind" in discovery_request.content
    assert report_request.method == "REPORT"
    assert report_request.headers["depth"] == "1"
    expected_auth = base64.b64encode(b"user:password").decode()
    assert discovery_request.headers["authorization"] == f"Basic {expected_auth}"
    assert report_request.headers["authorization"] == f"Basic {expected_auth}"
    assert b"addressbook-query" in report_request.content


def test_carddav_source_auto_negotiates_digest_auth() -> None:
    authorization_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        authorization_headers.append(authorization)
        if not authorization:
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": (
                        'Digest realm="BaikalDAV",qop="auth",nonce="test-nonce",'
                        'opaque="test-opaque",algorithm=MD5'
                    )
                },
            )
        assert authorization.startswith("Digest ")
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response><d:href>/contacts/</d:href><d:propstat><d:prop>
    <d:resourcetype><card:addressbook /></d:resourcetype>
  </d:prop></d:propstat></d:response>
</d:multistatus>""",
            )
        return httpx.Response(
            207,
            content=b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav" />""",
        )

    source = CardDAVContactSource(
        addressbook_url="https://baikal.example/contacts/",
        username="user",
        password="password",
        transport=httpx.MockTransport(handler),
    )

    contacts, truncated = source.list_contacts(limit=1)

    assert contacts == []
    assert truncated is False
    assert any(header.startswith("Digest ") for header in authorization_headers)


def test_private_contacts_server_places_raw_values_only_in_private_metadata() -> None:
    references = iter(("name-ref", "email-ref", "phone-ref"))
    server = PrivateContactsMCPServer(
        contacts=[
            Contact(
                name="Alice Smith",
                emails=("alice@example.com",),
                phones=("+1 555 0100",),
            )
        ],
        reference_factory=lambda: next(references),
    )

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "contacts_list", "arguments": {}},
        }
    )

    assert response is not None
    result = response["result"]
    assert result["structuredContent"] == {
        "contacts": [
            {
                "name": "{{pii:name:name-ref}}",
                "emails": ["{{pii:email:email-ref}}"],
                "phones": ["{{pii:phone:phone-ref}}"],
            }
        ],
        "truncated": False,
    }
    assert result["_meta"][PRIVATE_VALUES_META_KEY] == {
        "name-ref": "Alice Smith",
        "email-ref": "alice@example.com",
        "phone-ref": "+1 555 0100",
    }
    assert "Alice Smith" not in str(result["structuredContent"])
    assert "alice@example.com" not in str(result["structuredContent"])
    assert "+1 555 0100" not in str(result["structuredContent"])
