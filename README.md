# OpenCode MCP Gateway

Exposes OpenCode as a remote MCP server for Claude Code via `mcp.homunculi.cloud`.

## Architecture

```
┌─────────────┐         ┌──────────────────┐         ┌─────────────────┐
│Claude Code  │──HTTPS──│  Docker Caddy    │──HTTP───│  MCP Gateway   │
│(Researcher) │◀──SSE───│  mcp.homunculi. │         │  localhost:3001 │
│             │         │  cloud:443       │         │  (Python)       │
└─────────────┘         └──────────────────┘         └─────────────────┘
                                                          │
                                                          │ HTTP
                                                          ▼
                                               ┌─────────────────┐
                                               │  OpenCode API  │
                                               │  localhost:9999 │
                                               └─────────────────┘
```

## Features

- **OAuth 2.0 Authorization Server** - Works with Claude Code and ChatGPT Plus
- **Expanded Bash Surface** - Raw command execution + PTY lifecycle tools
- **Model Switching** - Support for multiple AI models per session
- **Protected Resource Metadata** - RFC 9728 compliance

## Available MCP Tools

### Session Management
| Tool | Description |
|------|-------------|
| `session_list` | List all OpenCode sessions |
| `session_create` | Create a new session |
| `session_get` | Get session details |
| `session_delete` | Delete a session |
| `session_fork` | Fork a session |
| `read_session` | Read session's full details |
| `switch_session` | Switch to a different session |
| `switch_model` | Set model for session |
| `get_active_session` | Get currently active session |

### Messaging (Agent Steering)
| Tool | Description |
|------|-------------|
| `message_send` | Send prompt to OpenCode agent |
| `message_abort` | Abort ongoing generation |

### Human Input Queue
| Tool | Description |
|------|-------------|
| `question_list` | List pending interactive questions |
| `question_reply` | Answer a queued question request |
| `question_reject` | Reject a queued question request |
| `permission_list` | List pending permission requests |
| `permission_reply` | Respond to queued permission request (`once`, `always`, `reject`) |

### Bash (Claude's Direct Terminal)
| Tool | Description |
|------|-------------|
| `bash` | Execute raw shell command (`command`, `timeout`, `workdir`, `description`) |
| `bash_exec` | Alias of `bash` |
| `bash_create` | Create PTY terminal |
| `bash_list` | List PTY sessions |
| `bash_get` | Get PTY details |
| `bash_read` | Read PTY output |
| `bash_resize` | Resize terminal |
| `bash_update` | Update PTY title/size |
| `bash_write` | Write input to PTY |
| `bash_close` | Close PTY |

### Status
| Tool | Description |
|------|-------------|
| `status` | Health check |

## Quick Start

```bash
cd /home/opencode/mcp-gateway
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Auto-Restart (systemd)

For VPS reliability, run the gateway as a systemd service with automatic restart.

```bash
cd /home/opencode/mcp-gateway
sudo cp deploy/systemd/opencode-mcp-gateway.service /etc/systemd/system/opencode-mcp-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now opencode-mcp-gateway
sudo systemctl status opencode-mcp-gateway --no-pager
```

Useful commands:

```bash
sudo systemctl restart opencode-mcp-gateway
sudo journalctl -u opencode-mcp-gateway -n 100 --no-pager
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_AUTH_TOKEN` | (auto-generated) | Bearer token |
| `MCP_CLIENT_ID` | opencode-mcp-gateway | OAuth client ID expected by this gateway |
| `OPENCODE_HOST` | localhost | OpenCode API host |
| `OPENCODE_PORT` | 9999 | OpenCode API port |
| `GATEWAY_PORT` | 3001 | Gateway HTTP port |
| `ENABLE_RAW_BASH` | true | Enables direct `bash`/`bash_exec` command tool |
| `PUBLIC_BASE_URL` | (auto-detected) | Override externally visible OAuth base URL (useful behind path prefixes like `/desktop`) |

## Second Connector On Same Domain

You can expose a second gateway on the same domain using a path prefix, for example `/desktop`.

- Run the second gateway with `PUBLIC_BASE_URL=https://mcp.homunculi.cloud/desktop`
- Route `https://mcp.homunculi.cloud/desktop/*` to that second gateway, stripping the `/desktop` prefix at the reverse proxy
- Register ChatGPT connector URL as `https://mcp.homunculi.cloud/desktop/mcp`

## Security Note

`bash`/`bash_exec` is remote code execution on the host machine. Keep this gateway personal-use and protect it with strong credentials.

## OAuth Endpoints

- Claude: `https://mcp.homunculi.cloud/.well-known/oauth-authorization-server`
- ChatGPT: `https://mcp.homunculi.cloud/.well-known/oauth-authorization-server/mcp`

## Running Tests

```bash
pip install pytest pytest-asyncio pytest-httpx
pytest tests/
```
