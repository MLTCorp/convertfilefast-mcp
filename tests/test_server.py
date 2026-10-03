import importlib.util
import sys
from pathlib import Path

import pytest


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
spec = importlib.util.spec_from_file_location("convertfilefast_mcp_server", SERVER_PATH)
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)


EXPECTED_CONVERSIONS = {
    "avif-to-jpg",
    "avif-to-pdf",
    "avif-to-png",
    "bmp-to-jpg",
    "bmp-to-pdf",
    "bmp-to-png",
    "csv-to-json",
    "csv-to-pdf",
    "csv-to-xlsx",
    "doc-to-pdf",
    "docx-to-pdf",
    "docx-to-txt",
    "heic-to-jpg",
    "heic-to-pdf",
    "heic-to-png",
    "html-to-pdf",
    "images-to-pdf",
    "jpg-to-pdf",
    "jpg-to-png",
    "jpg-to-webp",
    "json-to-csv",
    "markdown-to-pdf",
    "odt-to-pdf",
    "pdf-to-csv",
    "pdf-to-docx",
    "pdf-to-jpg",
    "pdf-to-png",
    "pdf-to-txt",
    "pdf-to-xlsx",
    "png-to-jpg",
    "png-to-pdf",
    "png-to-webp",
    "ppt-to-pdf",
    "pptx-to-pdf",
    "rtf-to-pdf",
    "svg-to-jpg",
    "svg-to-pdf",
    "svg-to-png",
    "tiff-to-jpg",
    "tiff-to-pdf",
    "tiff-to-png",
    "txt-to-pdf",
    "url-to-pdf",
    "webp-to-jpg",
    "webp-to-pdf",
    "webp-to-png",
    "xlsx-to-csv",
    "xlsx-to-pdf",
}


def test_supported_conversions_match_v2_surface():
    assert set(server.SUPPORTED_CONVERSIONS) == EXPECTED_CONVERSIONS


def test_missing_key_is_reported_before_request(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")
    result = server.compress_pdf(file_base64="not-used")
    assert result["status"] == "error"
    assert "Missing API key" in result["detail"]
    # The agent must be told how to get a key, not just that it lacks one.
    assert "/api/agent/provision" in result["get_api_key"]["autonomous_agent"]
    assert "/signup" in result["get_api_key"]["human"]


def test_401_from_api_includes_how_to_get_a_key():
    resp = server.httpx.Response(401, json={"detail": "API key required"})
    result = server._error(resp)
    assert result["http_status"] == 401
    assert "get_api_key" in result


def test_remote_delivery_returns_download_url(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REMOTE_MODE", True)
    monkeypatch.setattr(server, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(server, "DOWNLOAD_BASE", "https://mcp.example.test")
    monkeypatch.setattr(server, "DOWNLOAD_TTL_SECONDS", 60)

    result = server._deliver(b"hello", "../unsafe name.pdf")

    assert result["status"] == "success"
    assert result["download_url"].startswith("https://mcp.example.test/download/")
    assert result["filename"] == "unsafe name.pdf"
    assert list(tmp_path.glob("*/unsafe name.pdf"))


def test_base64_size_limit(monkeypatch):
    monkeypatch.setattr(server, "MAX_BASE64_BYTES", 3)
    result = server._decode_base64("aGVsbG8=", "sample")
    assert result["status"] == "error"
    assert "input limit" in result["detail"]


def test_report_feedback_posts_without_requiring_api_key(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "received", "category": "bug"}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, endpoint, data=None, files=None):
            calls.append({"endpoint": endpoint, "data": data, "files": files})
            return FakeResponse()

    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(server, "_client", lambda: FakeClient())

    result = server.report_feedback(
        summary="PDF output looked wrong",
        category="bug",
        tool="convert_file pdf-to-docx",
        context="Table alignment changed",
    )

    assert result["status"] == "success"
    assert result["result"]["status"] == "received"
    assert calls == [
        {
            "endpoint": "/v2/feedback",
            "data": {
                "summary": "PDF output looked wrong",
                "category": "bug",
                "tool": "convert_file pdf-to-docx",
                "context": "Table alignment changed",
            },
            "files": None,
        }
    ]


def test_billing_tools_call_api(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, endpoint):
            calls.append({"method": "GET", "endpoint": endpoint})
            return FakeResponse({"plan_id": "free", "total_available": 0})

        def post(self, endpoint, json=None, data=None, files=None):
            calls.append({"method": "POST", "endpoint": endpoint, "json": json})
            if endpoint.endswith("/portal"):
                return FakeResponse({"status": "success", "portal_url": "https://billing.stripe.com/session"})
            return FakeResponse({"status": "success", "checkout_url": "https://checkout.stripe.com/session"})

    monkeypatch.setattr(server, "_client", lambda: FakeClient())

    status = server.get_billing_status()
    subscription = server.create_subscription_checkout(plan_id="pro", annual=True, currency="usd")
    credits = server.create_credit_pack_checkout(pack_id="pack_1000", currency="brl")
    portal = server.create_billing_portal_link()

    assert status["result"]["plan_id"] == "free"
    assert subscription["result"]["checkout_url"].startswith("https://checkout.stripe.com/")
    assert credits["result"]["checkout_url"].startswith("https://checkout.stripe.com/")
    assert portal["result"]["portal_url"].startswith("https://billing.stripe.com/")
    assert calls == [
        {"method": "GET", "endpoint": "/v2/billing/status"},
        {
            "method": "POST",
            "endpoint": "/v2/billing/checkout/subscription",
            "json": {"plan_id": "pro", "annual": True, "currency": "usd"},
        },
        {
            "method": "POST",
            "endpoint": "/v2/billing/checkout/credits",
            "json": {"pack_id": "pack_1000", "currency": "brl"},
        },
        {"method": "POST", "endpoint": "/v2/billing/portal", "json": None},
    ]


# --- OAuth (US-013) -------------------------------------------------------
# These lock in the "off by default, must not break X-API-Key" contract from
# docs/exec-plans/plans/plan-003-oauth-mcp.md. They do not (and cannot,
# without a live Supabase project with the OAuth server enabled) exercise a
# real token exchange -- see that doc's "não testado" section.


def test_oauth_is_off_by_default_and_does_not_change_current_behavior():
    # This is the actual state server.py is imported with in this test file
    # (no CONVERTFILEFAST_OAUTH_ENABLED set), i.e. today's production config.
    assert server.OAUTH_ENABLED is False
    assert server.mcp.auth is None


def test_build_auth_provider_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(server, "OAUTH_ENABLED", False)
    monkeypatch.setattr(server, "SUPABASE_PROJECT_URL", "https://example.supabase.co")
    assert server._build_auth_provider() is None


def test_build_auth_provider_requires_project_url_when_enabled(monkeypatch):
    monkeypatch.setattr(server, "OAUTH_ENABLED", True)
    monkeypatch.setattr(server, "SUPABASE_PROJECT_URL", "")
    with pytest.raises(RuntimeError):
        server._build_auth_provider()


def test_build_auth_provider_builds_supabase_provider_when_enabled(monkeypatch):
    monkeypatch.setattr(server, "OAUTH_ENABLED", True)
    monkeypatch.setattr(server, "SUPABASE_PROJECT_URL", "https://example.supabase.co")
    monkeypatch.setattr(server, "DOWNLOAD_BASE", "https://mcp.convertfilefast.com")

    provider = server._build_auth_provider()

    assert isinstance(provider, server.SupabaseProvider)
    # Resource URL is what FastMCP advertises in RFC 9728 protected-resource
    # metadata and binds RequireAuthMiddleware to for the /mcp route.
    assert str(provider._get_resource_url("/mcp")) == "https://mcp.convertfilefast.com/mcp"
