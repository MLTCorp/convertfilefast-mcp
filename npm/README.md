# convertfilefast-mcp (npm launcher)

`npx` launcher for the ConvertFileFast MCP server.

This package is intentionally thin. It runs the Python package
`convertfilefast-mcp` from PyPI through `uvx`, forwarding stdio so MCP clients
can use the local stdio transport.

The MCP server exposes conversion tools, PDF/image operations, Stripe billing
link tools, and `report_feedback` for API/MCP issue reports.

You need `uv` installed:

<https://docs.astral.sh/uv/getting-started/installation/>

## Local stdio use

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

Get an API key at:

<https://www.convertfilefast.com/signup>

Prefer the hosted remote MCP server instead?

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

Full documentation:

<https://github.com/MLTCorp/convertfilefast-mcp>

---

mcp-name: io.github.MLTCorp/convertfilefast
