"""ConvertFileFast MCP server.

Exposes file conversion as tools for AI agents (Claude, Cursor, ChatGPT, ...).

The MCP server is a separate service from the REST backend: it talks to the
ConvertFileFast HTTP API and never imports the backend. It supports two
transports with the same tool surface:

* stdio/local: users run the package locally; results are saved to their disk.
* streamable-http/remote: ConvertFileFast hosts the MCP server; users pass their
  own API key in each request and receive temporary download links.

Auth:
  * local/stdio: CONVERTFILEFAST_API_KEY env var.
  * remote/http: X-API-Key or Authorization: Bearer cff_... request header.
  * OAuth (US-013, off by default): when CONVERTFILEFAST_OAUTH_ENABLED is set,
    the server also registers a Supabase OAuth resource-server provider
    (fastmcp.server.auth.providers.supabase.SupabaseProvider) so MCP clients
    (Claude, ChatGPT, ...) can authenticate via Supabase-issued OAuth tokens
    instead of a cff_ API key. IMPORTANT: this is a hard cutover for the
    /mcp route, not an additive option -- see
    docs/exec-plans/plans/plan-003-oauth-mcp.md for why cff_-only clients
    (X-API-Key with no Authorization header) would get a 401 once this is
    enabled, and why it must stay off until that is an accepted tradeoff.

Configuration:
  CONVERTFILEFAST_API_BASE               API base URL (default: production API)
  CONVERTFILEFAST_API_KEY                local stdio API key
  CONVERTFILEFAST_OUTPUT_DIR             local output dir (default ~/ConvertFileFast)
  CONVERTFILEFAST_TIMEOUT                per-request timeout in seconds (default 180)
  CONVERTFILEFAST_REMOTE_MODE            if true, return download URLs instead of paths
  CONVERTFILEFAST_DOWNLOAD_BASE          public base URL for remote download links
  CONVERTFILEFAST_TEMP_DIR               temporary remote file store (default /tmp/convertfilefast-mcp)
  CONVERTFILEFAST_DOWNLOAD_TTL           remote download TTL in seconds (default 3600)
  CONVERTFILEFAST_MAX_BASE64_MB          max decoded base64 input size (default 60)
  CONVERTFILEFAST_OAUTH_ENABLED          if true, register the Supabase OAuth provider (default: off)
  CONVERTFILEFAST_SUPABASE_PROJECT_URL   Supabase project URL; required when OAuth is enabled
"""

from __future__ import annotations

import base64
import ipaddress
import mimetypes
import os
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import quote, urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.providers.supabase import SupabaseProvider
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

API_BASE = os.environ.get("CONVERTFILEFAST_API_BASE", "https://api.convertfilefast.com").rstrip("/")
API_KEY = os.environ.get("CONVERTFILEFAST_API_KEY", "")
OUTPUT_DIR = Path(os.environ.get("CONVERTFILEFAST_OUTPUT_DIR", str(Path.home() / "ConvertFileFast")))
HTTP_TIMEOUT = float(os.environ.get("CONVERTFILEFAST_TIMEOUT", "180"))

REMOTE_MODE = os.environ.get("CONVERTFILEFAST_REMOTE_MODE", "").lower() in {"1", "true", "yes", "on"}
DOWNLOAD_BASE = os.environ.get("CONVERTFILEFAST_DOWNLOAD_BASE", "https://mcp.convertfilefast.com").rstrip("/")
TEMP_DIR = Path(os.environ.get("CONVERTFILEFAST_TEMP_DIR", "/tmp/convertfilefast-mcp"))
DOWNLOAD_TTL_SECONDS = int(os.environ.get("CONVERTFILEFAST_DOWNLOAD_TTL", "3600"))
MAX_BASE64_BYTES = int(float(os.environ.get("CONVERTFILEFAST_MAX_BASE64_MB", "60")) * 1024 * 1024)
SITE_URL = "https://www.convertfilefast.com"

# --- OAuth (US-013) -- off by default, see docstring above and
# docs/exec-plans/plans/plan-003-oauth-mcp.md for the coexistence tradeoff. ---
OAUTH_ENABLED = os.environ.get("CONVERTFILEFAST_OAUTH_ENABLED", "").lower() in {"1", "true", "yes", "on"}
SUPABASE_PROJECT_URL = os.environ.get("CONVERTFILEFAST_SUPABASE_PROJECT_URL", "")


def _build_auth_provider() -> SupabaseProvider | None:
    """Build the OAuth resource-server provider, or None when disabled (default).

    Deliberately off unless CONVERTFILEFAST_OAUTH_ENABLED is set, for two
    independent reasons:

    1. Supabase's OAuth server is not enabled yet for this project (its
       discovery endpoint 404s as of this writing) -- there is nothing to
       authenticate against today.
    2. Once fastmcp's `auth=` is set, it wraps *only* the MCP transport route
       (e.g. /mcp) with RequireAuthMiddleware, which demands a valid
       `Authorization: Bearer <token>` on every request, verified by this
       provider's single JWT verifier. Clients that authenticate with
       `X-API-Key: cff_...` and never send an `Authorization` header (today's
       primary documented auth style -- see module docstring) would get a
       401 at the middleware layer, before `_request_api_key()` or any tool
       code ever runs. Turning this flag on is therefore a hard cutover for
       the /mcp route, not an additive option. Flip it only when that
       tradeoff has been consciously accepted (or after a follow-up story
       that composes a second TokenVerifier for cff_ tokens via
       fastmcp.server.auth.MultiAuth -- confirmed to exist in fastmcp 3.4.2
       but not implemented here; untestable without a live OAuth server).
    """
    if not OAUTH_ENABLED:
        return None
    if not SUPABASE_PROJECT_URL:
        raise RuntimeError(
            "CONVERTFILEFAST_OAUTH_ENABLED is set but "
            "CONVERTFILEFAST_SUPABASE_PROJECT_URL is missing."
        )
    return SupabaseProvider(
        project_url=SUPABASE_PROJECT_URL,
        base_url=DOWNLOAD_BASE,  # public origin of this MCP server
        algorithm="ES256",  # confirmed: this Supabase project's JWKS responds 200 with 1 ES256 key
        resource_name="ConvertFileFast",
    )


mcp = FastMCP(
    "ConvertFileFast",
    auth=_build_auth_provider(),
    instructions=(
        "Convert files between 50+ formats, run PDF/image operations "
        "(merge, split, compress, rotate, protect, unlock, extract text, resize), "
        "report feedback, and create Stripe billing links. "
        "Prefer passing a public URL via `source_url`; otherwise pass base64. "
        "File-producing tools return a download URL (remote) or local path (stdio), never raw bytes. "
        "Each user authenticates with their own `cff_` API key. "
        "No key yet? A human signs up at https://www.convertfilefast.com/signup; an autonomous agent "
        "can POST https://www.convertfilefast.com/api/agent/provision to get one (starts with 0 credits). "
        "Any error caused by a missing key includes a `get_api_key` object with both paths. "
        "If a call fails with a credit or rate limit (HTTP 429), call `get_billing_status`, "
        "then `create_credit_pack_checkout` so the user can add prepaid credits and continue. "
        "Once a credit pack has been bought once, `manage_auto_recharge` can turn on automatic "
        "recharging so future purchases no longer need a human — call it with no arguments to see "
        "the current state and recent attempts, including a manual payment link for any attempt "
        "that needed authentication or was declined."
    ),
)

# --- Conversion matrix (mirrors the backend's /v2/convert/{from}-to-{to}) ---
_DOC_TO_PDF = ["docx", "doc", "xlsx", "pptx", "ppt", "rtf", "odt", "txt", "csv"]
_WEB_TO_PDF = ["html", "markdown", "url"]
_IMG_TO_PDF = ["jpg", "png", "heic", "webp", "avif", "svg", "bmp", "tiff"]
_PDF_TARGETS = ["docx", "txt", "csv", "xlsx", "jpg", "png"]
_IMG_TO_IMG = [
    "jpg-to-png",
    "png-to-jpg",
    "jpg-to-webp",
    "png-to-webp",
    "heic-to-jpg",
    "heic-to-png",
    "webp-to-jpg",
    "webp-to-png",
    "avif-to-jpg",
    "avif-to-png",
    "svg-to-png",
    "svg-to-jpg",
    "bmp-to-jpg",
    "bmp-to-png",
    "tiff-to-jpg",
    "tiff-to-png",
]
_DATA = ["csv-to-json", "json-to-csv", "csv-to-xlsx", "xlsx-to-csv"]
_DOC_TO_TEXT = ["docx-to-txt"]

VALID_SLUGS: set[str] = set(_IMG_TO_IMG) | set(_DATA) | set(_DOC_TO_TEXT)
for _s in _DOC_TO_PDF + _WEB_TO_PDF + _IMG_TO_PDF:
    VALID_SLUGS.add(f"{_s}-to-pdf")
for _t in _PDF_TARGETS:
    VALID_SLUGS.add(f"pdf-to-{_t}")

SUPPORTED_CONVERSIONS = sorted(VALID_SLUGS | {"images-to-pdf"})

_EXT_ALIAS = {"jpeg": "jpg", "tif": "tiff", "md": "markdown", "htm": "html", "text": "txt"}
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")

TargetFormat = Literal["pdf", "docx", "xlsx", "csv", "txt", "json", "jpg", "png", "webp"]
ImagePageSize = Literal["fit", "A4", "letter"]
FeedbackCategory = Literal["bug", "missing_conversion", "quality", "other"]
BillingPlan = Literal["pro", "scale"]
BillingCurrency = Literal["usd", "brl", "eur", "gbp"]
CreditPack = Literal["pack_1000", "pack_10000"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_remote_request() -> bool:
    if REMOTE_MODE:
        return True
    try:
        from fastmcp.server.dependencies import get_http_headers

        return bool(get_http_headers(include={"authorization"}))
    except Exception:
        return False


def _request_api_key() -> str:
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include={"authorization"})
    except Exception:
        headers = {}

    key = headers.get("x-api-key") or headers.get("convertfilefast-api-key") or ""
    auth = headers.get("authorization") or ""
    if not key and auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    return key


def _api_key() -> str:
    return _request_api_key() or API_KEY


# How to get a key, returned whenever a call fails for lack of one. Agents that
# hit the auth wall used to stop there and file feedback instead of signing up.
GET_API_KEY = {
    "human": f"Sign up at {SITE_URL}/signup (includes a free monthly allowance) and create a cff_ key in the dashboard.",
    "autonomous_agent": (
        f"POST {SITE_URL}/api/agent/provision (no auth) returns a cff_ key with 0 credits. "
        f"Then POST {SITE_URL}/api/agent/credits with header X-API-Key: <key> and JSON "
        '{"pack_id": "pack_1000"} to get a checkout link for prepaid credits.'
    ),
    "docs": f"{SITE_URL}/llms.txt",
}


def _missing_key_error() -> dict:
    if _is_remote_request():
        how_to_send = "Send it as X-API-Key: cff_... or Authorization: Bearer cff_... to the MCP server."
    else:
        how_to_send = "Set CONVERTFILEFAST_API_KEY to your cff_... key."
    return {
        "status": "error",
        "detail": f"Missing API key. {how_to_send}",
        "get_api_key": GET_API_KEY,
    }


def _client() -> httpx.Client:
    headers = {"User-Agent": "convertfilefast-mcp"}
    key = _api_key()
    if key:
        headers["X-API-Key"] = key
    return httpx.Client(base_url=API_BASE, headers=headers, timeout=HTTP_TIMEOUT)


def _formval(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _sanitize_filename(name: str) -> str:
    name = os.path.basename((name or "output").strip()).strip(". ")
    name = _SAFE_FILENAME_RE.sub("_", name)
    return name[:180] or "output"


def _filename_from_response(resp: httpx.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd)
    return _sanitize_filename(m.group(1)) if m else _sanitize_filename(fallback)


def _decode_base64(value: str, label: str = "file_base64") -> bytes | dict:
    approx_size = (len(value) * 3) // 4
    if approx_size > MAX_BASE64_BYTES:
        max_mb = MAX_BASE64_BYTES // (1024 * 1024)
        return {"status": "error", "detail": f"{label} exceeds the {max_mb}MB MCP input limit."}
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return {"status": "error", "detail": f"{label} is not valid base64."}


def _cleanup_expired_downloads() -> None:
    if not TEMP_DIR.exists():
        return
    cutoff = time.time() - DOWNLOAD_TTL_SECONDS
    for child in TEMP_DIR.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                for file_path in child.iterdir():
                    file_path.unlink(missing_ok=True)
                child.rmdir()
        except OSError:
            pass


def _deliver(content: bytes, out_name: str) -> dict:
    out_name = _sanitize_filename(out_name)
    if _is_remote_request():
        _cleanup_expired_downloads()
        token = secrets.token_urlsafe(32)
        out_dir = TEMP_DIR / token
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / out_name
        out_path.write_bytes(content)
        url = f"{DOWNLOAD_BASE}/download/{token}/{quote(out_name)}"
        return {
            "status": "success",
            "download_url": url,
            "filename": out_name,
            "bytes": len(content),
            "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
        }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / out_name
    out_path.write_bytes(content)
    return {"status": "success", "output_path": str(out_path), "filename": out_name, "bytes": len(content)}


def _error(resp: httpx.Response) -> dict:
    try:
        detail = resp.json().get("detail", resp.text[:300])
    except Exception:
        detail = resp.text[:300]
    hint = None
    if resp.status_code in (401, 403):
        hint = "Use a valid ConvertFileFast API key (cff_...)."
    elif resp.status_code == 429:
        hint = "Rate or credit limit reached. Add credits or slow down."
    error = {"status": "error", "http_status": resp.status_code, "detail": detail, "hint": hint}
    if resp.status_code == 401:
        error["get_api_key"] = GET_API_KEY
    return error


def _send(
    endpoint: str,
    *,
    default_out: str,
    source_url: Optional[str] = None,
    file_base64: Optional[str] = None,
    filename: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """POST to a conversion/operation endpoint with url-or-upload input."""
    if not _api_key():
        return _missing_key_error()

    data: dict[str, str] = {k: _formval(v) for k, v in (extra or {}).items() if v is not None}
    files = None
    if source_url:
        data["url"] = source_url
    elif file_base64:
        raw = _decode_base64(file_base64)
        if isinstance(raw, dict):
            return raw
        files = {"file": (filename or "input", raw)}
    else:
        return {"status": "error", "detail": "Provide either source_url or file_base64."}

    try:
        with _client() as c:
            resp = c.post(endpoint, data=data, files=files)
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}

    if resp.status_code != 200:
        return _error(resp)
    return _deliver(resp.content, _filename_from_response(resp, default_out))


def _guess_source_format(source_url: Optional[str], filename: Optional[str], target: str) -> Optional[str]:
    name = filename or (urlparse(source_url).path if source_url else "")
    ext = Path(name).suffix.lower().lstrip(".")
    ext = _EXT_ALIAS.get(ext, ext)
    if ext:
        return ext
    if source_url and target == "pdf":
        return "url"
    return None


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_public_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "URL must be http(s) and include a hostname."
    try:
        for result in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(result[4][0])
            if _is_blocked_ip(ip):
                return "URL resolves to a private or reserved network address."
    except OSError:
        return "Could not resolve URL hostname."
    return None


def _fetch_public_url(url: str, index: int) -> tuple[str, bytes] | dict:
    reason = _validate_public_url(url)
    if reason:
        return {"status": "error", "detail": f"source_urls[{index}] rejected: {reason}"}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as fetch:
            resp = fetch.get(url)
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Failed to fetch source_urls[{index}]: {e}"}
    if resp.status_code != 200:
        return {"status": "error", "detail": f"Could not fetch source_urls[{index}] (HTTP {resp.status_code})."}
    path_name = Path(urlparse(url).path).name or f"part{index + 1}"
    return (_sanitize_filename(path_name), resp.content)


def _json_or_error(resp: httpx.Response) -> dict:
    if resp.status_code != 200:
        return _error(resp)
    try:
        return {"status": "success", "result": resp.json()}
    except Exception:
        return {"status": "error", "detail": "API returned invalid JSON."}


# --------------------------------------------------------------------------- #
# HTTP routes for remote deployment
# --------------------------------------------------------------------------- #
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "convertfilefast-mcp"})


@mcp.custom_route("/download/{token}/{filename:path}", methods=["GET"])
async def download_file(request: Request) -> Response:
    token = request.path_params["token"]
    filename = _sanitize_filename(request.path_params["filename"])
    if not _TOKEN_RE.match(token):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    out_dir = TEMP_DIR / token
    out_path = out_dir / filename
    try:
        resolved = out_path.resolve()
        root = TEMP_DIR.resolve()
    except OSError:
        return JSONResponse({"detail": "Not found"}, status_code=404)

    if root not in resolved.parents or not out_path.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if out_dir.stat().st_mtime < time.time() - DOWNLOAD_TTL_SECONDS:
        return JSONResponse({"detail": "Download expired"}, status_code=410)

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(out_path, media_type=media_type, filename=filename)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def list_supported_conversions() -> dict:
    """List every conversion slug supported by the MCP server."""
    return {
        "status": "success",
        "conversions": SUPPORTED_CONVERSIONS,
        "count": len(SUPPORTED_CONVERSIONS),
        "api_base": API_BASE,
    }


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def convert_file(
    target_format: Annotated[TargetFormat, Field(description="Desired output format, e.g. 'pdf', 'csv', 'png'.")],
    source_url: Annotated[Optional[str], Field(description="Public URL to the source file or webpage (preferred). For a webpage to PDF, point at the page and use target_format='pdf'.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded source file. Use only when there is no URL.")] = None,
    filename: Annotated[Optional[str], Field(description="Original filename (with extension); used to detect the source format and name the output.")] = None,
    source_format: Annotated[Optional[str], Field(description="Source format token (e.g. 'docx', 'pdf', 'png', 'html', 'url'). Inferred from filename/URL if omitted.")] = None,
) -> dict:
    """Convert a document, spreadsheet, presentation, image, PDF, or data file."""
    src = (source_format or _guess_source_format(source_url, filename, target_format) or "").lower()
    src = _EXT_ALIAS.get(src, src)
    if not src:
        return {"status": "error", "detail": "Could not determine the source format. Pass `source_format` or a `filename` with an extension."}

    slug = f"{src}-to-{target_format}"
    if slug not in VALID_SLUGS:
        valid_targets = sorted(s.split("-to-")[1] for s in VALID_SLUGS if s.startswith(f"{src}-to-"))
        return {"status": "error", "detail": f"Conversion '{slug}' is not supported.", "supported_targets_for_source": valid_targets or None}

    stem = Path(filename).stem if filename else "converted"
    return _send(f"/v2/convert/{slug}", default_out=f"{stem}.{target_format}",
                 source_url=source_url, file_base64=file_base64, filename=filename)


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def images_to_pdf(
    source_urls: Annotated[Optional[list[str]], Field(description="Public image URLs to combine into a multi-page PDF, in order.")] = None,
    files_base64: Annotated[Optional[list[str]], Field(description="Base64-encoded image files to combine, in order.")] = None,
    filenames: Annotated[Optional[list[str]], Field(description="Optional filenames matching files_base64, with extensions.")] = None,
    page_size: Annotated[ImagePageSize, Field(description="Page size: fit, A4, or letter.")] = "fit",
    output_name: Annotated[str, Field(description="Name for the output PDF.")] = "images.pdf",
) -> dict:
    """Combine multiple images into one multi-page PDF."""
    if not _api_key():
        return _missing_key_error()

    blobs: list[tuple[str, bytes]] = []
    if files_base64:
        for i, value in enumerate(files_base64):
            raw = _decode_base64(value, f"files_base64[{i}]")
            if isinstance(raw, dict):
                return raw
            name = filenames[i] if filenames and i < len(filenames) else f"image{i + 1}.png"
            blobs.append((_sanitize_filename(name), raw))
    elif source_urls:
        for i, url in enumerate(source_urls):
            fetched = _fetch_public_url(url, i)
            if isinstance(fetched, dict):
                return fetched
            blobs.append(fetched)
    else:
        return {"status": "error", "detail": "Provide source_urls or files_base64."}

    if not blobs:
        return {"status": "error", "detail": "At least one image is required."}

    files = [("files", (name, data)) for name, data in blobs]
    try:
        with _client() as c:
            resp = c.post("/v2/convert/images-to-pdf", data={"page_size": page_size}, files=files)
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}
    if resp.status_code != 200:
        return _error(resp)
    return _deliver(resp.content, _filename_from_response(resp, output_name))


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def merge_pdfs(
    source_urls: Annotated[Optional[list[str]], Field(description="Public URLs of the PDFs to merge, in order (minimum 2).")] = None,
    files_base64: Annotated[Optional[list[str]], Field(description="Base64-encoded PDFs to merge, in order (minimum 2). Use when there are no URLs.")] = None,
    output_name: Annotated[str, Field(description="Name for the merged PDF.")] = "merged.pdf",
) -> dict:
    """Merge multiple PDFs into a single PDF, preserving the given order."""
    if not _api_key():
        return _missing_key_error()

    blobs: list[tuple[str, bytes]] = []
    if files_base64:
        for i, value in enumerate(files_base64):
            raw = _decode_base64(value, f"files_base64[{i}]")
            if isinstance(raw, dict):
                return raw
            blobs.append((f"part{i + 1}.pdf", raw))
    elif source_urls:
        for i, url in enumerate(source_urls):
            fetched = _fetch_public_url(url, i)
            if isinstance(fetched, dict):
                return fetched
            name, data = fetched
            blobs.append((name if name.lower().endswith(".pdf") else f"part{i + 1}.pdf", data))
    else:
        return {"status": "error", "detail": "Provide source_urls or files_base64 (at least 2 PDFs)."}

    if len(blobs) < 2:
        return {"status": "error", "detail": "Need at least 2 PDFs to merge."}

    files = [("files", (name, data)) for name, data in blobs]
    try:
        with _client() as c:
            resp = c.post("/v2/pdf/merge", files=files)
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}
    if resp.status_code != 200:
        return _error(resp)
    return _deliver(resp.content, _filename_from_response(resp, output_name))


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def split_pdf(
    pages: Annotated[str, Field(description="Pages/ranges to extract, e.g. '1,3,5-7' or '1-5'.")],
    source_url: Annotated[Optional[str], Field(description="Public URL of the PDF.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded PDF.")] = None,
) -> dict:
    """Extract specific pages/ranges from a PDF."""
    return _send("/v2/pdf/split", default_out="split.zip", source_url=source_url,
                 file_base64=file_base64, extra={"pages": pages})


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def compress_pdf(
    source_url: Annotated[Optional[str], Field(description="Public URL of the PDF.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded PDF.")] = None,
    quality: Annotated[Literal["low", "medium", "high", "maximum"], Field(description="Compression preset.")] = "medium",
    remove_metadata: Annotated[bool, Field(description="Strip PDF metadata to reduce size further.")] = False,
) -> dict:
    """Reduce the file size of a PDF."""
    return _send("/v2/pdf/compress", default_out="compressed.pdf", source_url=source_url,
                 file_base64=file_base64, extra={"quality": quality, "remove_metadata": remove_metadata})


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def rotate_pdf(
    angle: Annotated[Literal[90, 180, 270], Field(description="Rotation in degrees (clockwise).")],
    source_url: Annotated[Optional[str], Field(description="Public URL of the PDF.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded PDF.")] = None,
    pages: Annotated[Optional[str], Field(description="Pages to rotate, e.g. '1,3,5-7'. Omit to rotate all pages.")] = None,
) -> dict:
    """Rotate pages in a PDF."""
    return _send("/v2/pdf/rotate", default_out="rotated.pdf", source_url=source_url,
                 file_base64=file_base64, extra={"angle": angle, "pages": pages})


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def protect_pdf(
    password: Annotated[str, Field(description="Password to set on the PDF.")],
    source_url: Annotated[Optional[str], Field(description="Public URL of the PDF.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded PDF.")] = None,
) -> dict:
    """Add password protection (encryption) to a PDF."""
    return _send("/v2/pdf/protect", default_out="protected.pdf", source_url=source_url,
                 file_base64=file_base64, extra={"password": password})


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def unlock_pdf(
    password: Annotated[str, Field(description="Current password of the PDF.")],
    source_url: Annotated[Optional[str], Field(description="Public URL of the PDF.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded PDF.")] = None,
) -> dict:
    """Remove password protection from a PDF."""
    return _send("/v2/pdf/unlock", default_out="unlocked.pdf", source_url=source_url,
                 file_base64=file_base64, extra={"password": password})


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def extract_pdf_text(
    source_url: Annotated[Optional[str], Field(description="Public URL of the PDF.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded PDF.")] = None,
    pages: Annotated[Optional[str], Field(description="Optional pages/ranges to extract, e.g. '1-5'.")] = None,
    extract_tables: Annotated[bool, Field(description="Whether to extract tables too.")] = True,
) -> dict:
    """Extract text and optional tables from a PDF as structured JSON."""
    if not _api_key():
        return _missing_key_error()

    data: dict[str, str] = {"extract_tables": _formval(extract_tables)}
    if pages:
        data["pages"] = pages
    files = None
    if source_url:
        data["url"] = source_url
    elif file_base64:
        raw = _decode_base64(file_base64)
        if isinstance(raw, dict):
            return raw
        files = {"file": ("input.pdf", raw)}
    else:
        return {"status": "error", "detail": "Provide either source_url or file_base64."}

    try:
        with _client() as c:
            resp = c.post("/v2/pdf/extract-text", data=data, files=files)
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}
    return _json_or_error(resp)


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def resize_image(
    source_url: Annotated[Optional[str], Field(description="Public URL of the image.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded image.")] = None,
    width: Annotated[Optional[int], Field(description="Target width in pixels.")] = None,
    height: Annotated[Optional[int], Field(description="Target height in pixels.")] = None,
    fit: Annotated[Literal["contain", "cover", "fill"], Field(description="How to fit within width/height.")] = "contain",
    format: Annotated[Optional[str], Field(description="Output format (jpeg, png, webp, ...). Defaults to source format.")] = None,
) -> dict:
    """Resize an image to the given width/height."""
    if width is None and height is None:
        return {"status": "error", "detail": "Provide at least one of width or height."}
    return _send("/v2/image/resize", default_out="resized.png", source_url=source_url,
                 file_base64=file_base64, extra={"width": width, "height": height, "fit": fit, "format": format})


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def compress_image(
    source_url: Annotated[Optional[str], Field(description="Public URL of the image.")] = None,
    file_base64: Annotated[Optional[str], Field(description="Base64-encoded image.")] = None,
    quality: Annotated[int, Field(description="Compression quality 1-100.", ge=1, le=100)] = 85,
    format: Annotated[Optional[str], Field(description="Output format (jpeg, png, webp, ...). Defaults to source format.")] = None,
    max_width: Annotated[Optional[int], Field(description="Optionally cap the width in pixels.")] = None,
    max_height: Annotated[Optional[int], Field(description="Optionally cap the height in pixels.")] = None,
) -> dict:
    """Compress an image to reduce file size, optionally capping dimensions."""
    return _send("/v2/image/compress", default_out="compressed.png", source_url=source_url,
                 file_base64=file_base64,
                 extra={"quality": quality, "format": format, "max_width": max_width, "max_height": max_height})


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
def get_billing_status() -> dict:
    """Return current plan, subscription status, and remaining credits."""
    try:
        with _client() as c:
            resp = c.get("/v2/billing/status")
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}

    return _json_or_error(resp)


@mcp.tool(annotations={"idempotentHint": False, "openWorldHint": True})
def create_subscription_checkout(
    plan_id: Annotated[BillingPlan, Field(description="Plan to subscribe to: pro or scale.")],
    annual: Annotated[bool, Field(description="Use annual billing instead of monthly.")] = False,
    currency: Annotated[BillingCurrency, Field(description="Preferred checkout currency.")] = "usd",
) -> dict:
    """Create a Stripe-hosted checkout link for a Pro or Scale subscription."""
    try:
        with _client() as c:
            resp = c.post(
                "/v2/billing/checkout/subscription",
                json={"plan_id": plan_id, "annual": annual, "currency": currency},
            )
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}

    return _json_or_error(resp)


@mcp.tool(annotations={"idempotentHint": False, "openWorldHint": True})
def create_credit_pack_checkout(
    pack_id: Annotated[CreditPack, Field(description="Credit pack to buy: pack_1000 or pack_10000.")],
    currency: Annotated[BillingCurrency, Field(description="Preferred checkout currency.")] = "usd",
) -> dict:
    """Create a Stripe-hosted checkout link for one-time extra credits."""
    try:
        with _client() as c:
            resp = c.post(
                "/v2/billing/checkout/credits",
                json={"pack_id": pack_id, "currency": currency},
            )
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}

    return _json_or_error(resp)


@mcp.tool(annotations={"idempotentHint": False, "openWorldHint": True})
def create_billing_portal_link() -> dict:
    """Create a Stripe Billing Portal link for payment method and subscription management."""
    try:
        with _client() as c:
            resp = c.post("/v2/billing/portal")
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}

    return _json_or_error(resp)


@mcp.tool(annotations={"idempotentHint": False, "openWorldHint": True})
def manage_auto_recharge(
    enabled: Annotated[Optional[bool], Field(description="Turn automatic credit recharge on (true) or off (false). Omit to leave unchanged.")] = None,
    threshold_credits: Annotated[Optional[int], Field(description="Balance at which auto-recharge should fire. Omit to leave unchanged.", ge=0)] = None,
) -> dict:
    """Check or change automatic credit-pack recharge: on/off, the trigger threshold, and
    the last recharge attempts (including the manual payment link when a charge required
    authentication or was declined). Call with no arguments to just check the current state."""
    try:
        with _client() as c:
            if enabled is None and threshold_credits is None:
                resp = c.get("/v2/billing/auto-recharge")
            else:
                body: dict = {}
                if enabled is not None:
                    body["enabled"] = enabled
                if threshold_credits is not None:
                    body["threshold_credits"] = threshold_credits
                resp = c.post("/v2/billing/auto-recharge", json=body)
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}

    return _json_or_error(resp)


@mcp.tool(annotations={"idempotentHint": False, "openWorldHint": True})
def report_feedback(
    summary: Annotated[str, Field(description="Short description of what went wrong or what is missing.", min_length=3, max_length=1000)],
    category: Annotated[FeedbackCategory, Field(description="Feedback category.")] = "other",
    tool: Annotated[Optional[str], Field(description="Tool or conversion involved, e.g. 'convert_file pdf-to-docx'.")] = None,
    context: Annotated[Optional[str], Field(description="Extra detail: error message, input format, expected vs actual.")] = None,
) -> dict:
    """Report a bug, missing conversion, or quality issue to the ConvertFileFast team."""
    data = {
        "summary": summary[:1000],
        "category": category,
    }
    if tool:
        data["tool"] = tool[:200]
    if context:
        data["context"] = context[:2000]

    try:
        with _client() as c:
            resp = c.post("/v2/feedback", data=data)
    except httpx.RequestError as e:
        return {"status": "error", "detail": f"Request to API failed: {e}"}

    return _json_or_error(resp)


def main() -> None:
    """Console-script / module entry point."""
    transport = os.environ.get("CONVERTFILEFAST_MCP_TRANSPORT", "stdio").lower()
    if transport in {"http", "streamable-http", "sse"}:
        host = os.environ.get("HOST", os.environ.get("CONVERTFILEFAST_MCP_HOST", "0.0.0.0"))
        port = int(os.environ.get("PORT", os.environ.get("CONVERTFILEFAST_MCP_PORT", "8000")))
        path = os.environ.get("CONVERTFILEFAST_MCP_PATH", "/mcp")
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            path=path,
            stateless_http=os.environ.get("CONVERTFILEFAST_MCP_STATELESS", "true").lower() in {"1", "true", "yes", "on"},
        )
        return
    mcp.run()


if __name__ == "__main__":
    main()
