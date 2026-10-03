#!/usr/bin/env node
"use strict";

// Thin launcher: runs the ConvertFileFast MCP server (a Python package published
// on PyPI as `convertfilefast-mcp`) via `uvx`, forwarding stdio so the MCP stdio
// protocol passes straight through. Requires `uv` (which provides `uvx`).

const { spawnSync } = require("node:child_process");

const PKG = "convertfilefast-mcp";
const passthrough = process.argv.slice(2);

function run(useShell) {
  return spawnSync("uvx", [PKG, ...passthrough], { stdio: "inherit", shell: useShell });
}

let res = run(false);
if (res.error && res.error.code === "ENOENT") {
  res = run(true); // some Windows setups need shell resolution for uvx
}

if (res.error) {
  console.error(
    "[convertfilefast-mcp] Could not start — this launcher needs 'uv' (which provides 'uvx').\n" +
      "Install uv: https://docs.astral.sh/uv/getting-started/installation/\n" +
      "Or run the server directly: uvx convertfilefast-mcp"
  );
  process.exit(1);
}
process.exit(res.status === null ? 1 : res.status);
