# google-keep-mcp

An MCP server for Google Keep, built on [gkeepapi](https://github.com/kiwiz/gkeepapi) (an unofficial, reverse-engineered client — there is no official Google Keep API for consumer accounts).

## Tools

16 tools across three groups:

**Notes & lists** — `search_notes`, `get_note`, `create_note`, `create_list`, `update_note`, `trash_note`, `restore_note`, `delete_note`

**Checklist items** — `add_list_item`, `update_list_item`, `delete_list_item`

**Labels** — `list_labels`, `create_label`, `delete_label`, `add_label_to_note`, `remove_label_from_note`

## Auth

`gkeepapi` authenticates with an email + master token (not your Google password, not an app password long-term). Get one:

```bash
uv run python scripts/get_master_token.py
```

This trades a Google **App Password** (requires 2FA on the account) for a master token via `gpsoauth`, one time. It also pins a random device ID so the server doesn't rely on the host machine's MAC address for its device identity — needed since this runs in a container, not on a fixed physical device.

Required env vars:

```bash
GOOGLE_KEEP_EMAIL=you@gmail.com
GOOGLE_KEEP_MASTER_TOKEN=...
GOOGLE_KEEP_DEVICE_ID=...

# optional: cache Keep state to disk for faster startup
GOOGLE_KEEP_STATE_PATH=./.keep_state.json
```

See `.env.example`.

## Quick Start

```bash
uv sync

# Run locally (stdio)
uv run python -m google_keep_mcp.server

# Run as HTTP server
uv run python -m google_keep_mcp.server --transport streamable-http --host 0.0.0.0 --port 8080
```

## Docker

Pre-built multi-arch images (amd64/arm64) are published to GHCR on every push to `main`:

```bash
docker pull ghcr.io/suckerfish/google_keep_mcp:latest
```

Or `docker compose up` using `compose.yaml`. Health check at `GET /health`.

## Deployment

Runs on `ampere` via Komodo (stack: `google-keep-mcp-ampere`), registered in MetaMCP's `main-namespace` as `google-keep-mcp` (port 8082). Credentials are injected via Komodo secret variables (`[[GOOGLE_KEEP_EMAIL]]`, `[[GOOGLE_KEEP_MASTER_TOKEN]]`, `[[GOOGLE_KEEP_DEVICE_ID]]`) — never stored in the stack config or this repo. `GOOGLE_KEEP_STATE_PATH` points at a mounted volume so the Keep state cache survives container restarts.

Pushing to `main` rebuilds the image; redeploy the stack via Komodo to pick it up (`auto_pull: true`, but a fresh deploy still needs to be triggered — it doesn't auto-redeploy on push).

## A note on stability

`gkeepapi` is unofficial and periodically breaks when Google changes its login flow or adds new Keep content types — see its [CHANGELOG](https://github.com/kiwiz/gkeepapi/blob/master/CHANGELOG.md) and issue tracker. If this server suddenly starts failing with `LoginException` or similar, it's very likely a `gkeepapi` compatibility issue, not something wrong in this repo. Check upstream before debugging locally.

## Tech Stack

| Component | Choice |
|-----------|--------|
| MCP framework | [FastMCP](https://gofastmcp.com) 2.x |
| Keep client | [gkeepapi](https://github.com/kiwiz/gkeepapi) |
| Package management | [uv](https://github.com/astral-sh/uv) |
