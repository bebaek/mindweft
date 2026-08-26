from fastapi.testclient import TestClient

from app.llm import MockLLMAdapter
from app.main import create_app
from app.security_headers import WEB_CONTENT_SECURITY_POLICY
from app.tools import build_local_tool_registry


def _client() -> TestClient:
    return TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )


def test_common_security_headers_apply_to_api_responses() -> None:
    with _client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["permissions-policy"] == (
        "camera=(self), microphone=(self), geolocation=()"
    )
    assert response.headers["x-permitted-cross-domain-policies"] == "none"
    assert "content-security-policy" not in response.headers


def test_browser_clients_use_restrictive_content_security_policy() -> None:
    with _client() as client:
        document = client.get("/web/")
        script = client.get("/web/app.js")
        console = client.get("/console/")

    assert document.status_code == 200
    assert script.status_code == 200
    assert console.status_code == 200
    assert document.headers["content-security-policy"] == WEB_CONTENT_SECURITY_POLICY
    assert script.headers["content-security-policy"] == WEB_CONTENT_SECURITY_POLICY
    assert console.headers["content-security-policy"] == WEB_CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in WEB_CONTENT_SECURITY_POLICY
    assert "object-src 'none'" in WEB_CONTENT_SECURITY_POLICY
    assert "frame-src 'self' blob:" in WEB_CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in WEB_CONTENT_SECURITY_POLICY
    assert "img-src 'self' blob: data:" in WEB_CONTENT_SECURITY_POLICY
    assert (
        "unsafe-inline"
        not in WEB_CONTENT_SECURITY_POLICY.split("script-src", 1)[1].split(";", 1)[0]
    )
