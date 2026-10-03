# ConvertFileFast MCP server

[![PyPI version](https://img.shields.io/pypi/v/convertfilefast-mcp?label=PyPI)](https://pypi.org/project/convertfilefast-mcp/)
[![npm version](https://img.shields.io/npm/v/convertfilefast-mcp?label=npm)](https://www.npmjs.com/package/convertfilefast-mcp)
[![PyPI downloads](https://img.shields.io/pypi/dm/convertfilefast-mcp?label=downloads)](https://pypi.org/project/convertfilefast-mcp/)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.MLTCorp%2Fconvertfilefast-blue)](https://registry.modelcontextprotocol.io)

MCP server for ConvertFileFast. It lets AI agents convert files, run PDF/image
operations, create Stripe billing links, and report feedback through the
ConvertFileFast REST API.

## Why agents pick it

- **One tool covers 50+ conversions** — `convert_file` routes by target format, so the model says "I have a PPTX URL, give me a PDF" instead of memorizing slugs.
- **URL-in, URL-out** — pass a public `source_url`, get a short-lived `download_url`; binaries never sit in the agent's context window.
- **Pay per use, cheap** — prepaid credits from ~$0.003/conversion, 10 free/month. No seat licenses.
- **Safe by design** — per-user `cff_` API keys (never shared/stored by the remote server), SSRF-validated URL inputs, deterministic server-side conversion.
- **Self-serve top-up** — when credits run out, the agent calls `get_billing_status` + `create_credit_pack_checkout` and returns a Stripe link.

## Use it from an agent

After adding the server (see install below), prompts like these just work:

- "Convert https://example.com/contract.docx to PDF."
- "Merge these three PDF URLs into one file, in order."
- "This PDF is 40 MB — compress it to medium quality and give me a link."
- "Extract the tables from this PDF as JSON."
- "Resize this PNG to 1200px wide and return a WebP."

The same tools are available in both modes:

- Local stdio: the MCP server runs on the user's machine and saves results to a
  local folder.
- Remote HTTP: ConvertFileFast hosts the MCP server at
  `https://mcp.convertfilefast.com/mcp` and returns temporary download links.

## Tools

| Tool | API endpoint(s) | Result |
| --- | --- | --- |
| `list_supported_conversions` | local matrix | JSON list |
| `convert_file` | `/v2/convert/{from}-to-{to}` | File |
| `images_to_pdf` | `/v2/convert/images-to-pdf` | PDF |
| `merge_pdfs` | `/v2/pdf/merge` | PDF |
| `split_pdf` | `/v2/pdf/split` | ZIP |
| `compress_pdf` | `/v2/pdf/compress` | PDF |
| `rotate_pdf` | `/v2/pdf/rotate` | PDF |
| `protect_pdf` | `/v2/pdf/protect` | PDF |
| `unlock_pdf` | `/v2/pdf/unlock` | PDF |
| `extract_pdf_text` | `/v2/pdf/extract-text` | JSON |
| `resize_image` | `/v2/image/resize` | Image |
| `compress_image` | `/v2/image/compress` | Image |
| `get_billing_status` | `/v2/billing/status` | JSON |
| `create_subscription_checkout` | `/v2/billing/checkout/subscription` | Stripe Checkout URL |
| `create_credit_pack_checkout` | `/v2/billing/checkout/credits` | Stripe Checkout URL |
| `create_billing_portal_link` | `/v2/billing/portal` | Stripe Portal URL |
| `report_feedback` | `/v2/feedback` | JSON |

Every file-producing tool accepts a public `source_url` where possible, or
base64 content when the client cannot expose a URL.
Billing tools never collect card data; they return Stripe-hosted URLs for the
user to open and confirm payment.
`report_feedback` accepts a short summary, category, related tool, and context;
it can run without an API key, but forwards one when the client provides it.

## Supported conversions

Use the `list_supported_conversions` tool for the canonical list. Current slugs:

`avif-to-jpg`, `avif-to-pdf`, `avif-to-png`, `bmp-to-jpg`, `bmp-to-pdf`,
`bmp-to-png`, `csv-to-json`, `csv-to-pdf`, `csv-to-xlsx`, `doc-to-pdf`,
`docx-to-pdf`, `docx-to-txt`, `heic-to-jpg`, `heic-to-pdf`, `heic-to-png`, `html-to-pdf`,
`images-to-pdf`, `jpg-to-pdf`, `jpg-to-png`, `jpg-to-webp`, `json-to-csv`,
`markdown-to-pdf`, `odt-to-pdf`, `pdf-to-csv`, `pdf-to-docx`, `pdf-to-jpg`,
`pdf-to-png`, `pdf-to-txt`, `pdf-to-xlsx`, `png-to-jpg`, `png-to-pdf`,
`png-to-webp`, `ppt-to-pdf`, `pptx-to-pdf`, `rtf-to-pdf`, `svg-to-jpg`,
`svg-to-pdf`, `svg-to-png`, `tiff-to-jpg`, `tiff-to-pdf`, `tiff-to-png`,
`txt-to-pdf`, `url-to-pdf`, `webp-to-jpg`, `webp-to-pdf`, `webp-to-png`,
`xlsx-to-csv`, `xlsx-to-pdf`.

## Authentication

Create a ConvertFileFast API key at:

<https://www.convertfilefast.com/signup>

Local stdio uses:

```text
CONVERTFILEFAST_API_KEY=cff_...
```

Remote HTTP uses either:

```text
X-API-Key: cff_...
```

or:

```text
Authorization: Bearer cff_...
```

The remote MCP server does not store API keys. It forwards the key to
`api.convertfilefast.com` for each tool call.

## Remote MCP

Use this when the MCP client supports remote Streamable HTTP servers:

```json
{
  "mcpServers": {
    "convertfilefast": {
      "url": "https://mcp.convertfilefast.com/mcp",
      "headers": {
        "X-API-Key": "cff_REPLACE_WITH_YOUR_KEY"
      }
    }
  }
}
```

Remote file results look like:

```json
{
  "status": "success",
  "download_url": "https://mcp.convertfilefast.com/download/...",
  "filename": "converted.pdf",
  "bytes": 12345,
  "expires_in_seconds": 3600
}
```

## Local stdio install

Recommended:

```json
{
  "mcpServers": {
    "convertfilefast": {
      "command": "uvx",
      "args": ["convertfilefast-mcp"],
      "env": {
        "CONVERTFILEFAST_API_KEY": "cff_REPLACE_WITH_YOUR_KEY"
      }
    }
  }
}
```

Alternative via npm launcher:

```json
{
  "mcpServers": {
    "convertfilefast": {
      "command": "npx",
      "args": ["-y", "convertfilefast-mcp"],
      "env": {
        "CONVERTFILEFAST_API_KEY": "cff_REPLACE_WITH_YOUR_KEY"
      }
    }
  }
}
```

Local file results look like:

```json
{
  "status": "success",
  "output_path": "/Users/me/ConvertFileFast/converted.pdf",
  "filename": "converted.pdf",
  "bytes": 12345
}
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONVERTFILEFAST_API_BASE` | `https://api.convertfilefast.com` | REST API base URL |
| `CONVERTFILEFAST_API_KEY` | empty | Local stdio API key |
| `CONVERTFILEFAST_OUTPUT_DIR` | `~/ConvertFileFast` | Local output directory |
| `CONVERTFILEFAST_TIMEOUT` | `180` | Per-request timeout in seconds |
| `CONVERTFILEFAST_MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http`, `http`, or `sse` |
| `CONVERTFILEFAST_MCP_HOST` | `0.0.0.0` | HTTP bind host |
| `CONVERTFILEFAST_MCP_PORT` | `8000` | HTTP bind port |
| `CONVERTFILEFAST_MCP_PATH` | `/mcp` | HTTP MCP endpoint |
| `CONVERTFILEFAST_REMOTE_MODE` | false | Return temporary download URLs |
| `CONVERTFILEFAST_DOWNLOAD_BASE` | `https://mcp.convertfilefast.com` | Public base URL for downloads |
| `CONVERTFILEFAST_TEMP_DIR` | `/tmp/convertfilefast-mcp` | Remote temporary file store |
| `CONVERTFILEFAST_DOWNLOAD_TTL` | `3600` | Download URL TTL in seconds |
| `CONVERTFILEFAST_MAX_BASE64_MB` | `60` | Max decoded base64 payload size |

## Local development

```bash
uv run --with fastmcp --with httpx --with pydantic python server.py
```

Run tests:

```bash
uv run --with fastmcp --with httpx --with pydantic --with pytest pytest tests -q
```

Run remote mode locally:

```bash
CONVERTFILEFAST_MCP_TRANSPORT=streamable-http \
CONVERTFILEFAST_REMOTE_MODE=true \
CONVERTFILEFAST_DOWNLOAD_BASE=http://127.0.0.1:8000 \
uv run --with fastmcp --with httpx --with pydantic python server.py
```

Then check:

```bash
curl http://127.0.0.1:8000/health
```

---

mcp-name: io.github.MLTCorp/convertfilefast
