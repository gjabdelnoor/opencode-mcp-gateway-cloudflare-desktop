# OpenCode MCP Gateway Cloudflare Desktop

> [!WARNING]
> This project exposes remote shell execution, PTY control, session steering, and agent-driven code execution on the machine where it runs.
> Treat it like a personal-use remote code execution service.
> If this gateway is compromised, an attacker may be able to read files, execute commands, access credentials, damage data, or pivot deeper into your environment.
> Do not expose it to untrusted users. Use strong secrets. Keep the origin machine locked down.

This repository is the Cloudflare desktop deployment variant of `opencode-mcp-gateway`.

It is designed for:

- an Ubuntu desktop or laptop
- a local OpenCode server
- a public HTTPS MCP endpoint fronted by Cloudflare Tunnel
- Claude and ChatGPT OAuth-compatible remote MCP usage
- no VPS

Repository:

- `https://github.com/gjabdelnoor/opencode-mcp-gateway-cloudflare-desktop`


## Architecture

```text
Claude / ChatGPT
        |
        v
  https://mcp.example.com/mcp
        |
        v
   Cloudflare Edge
        |
        v
   cloudflared tunnel
        |
        v
  http://127.0.0.1:3001
        |
        v
  http://127.0.0.1:9999
        |
        v
      OpenCode
```

## Read This First

You need OpenCode installed and working before this gateway can do anything useful.

OpenCode docs:

- Intro: `https://opencode.ai/docs/`
- Providers: `https://opencode.ai/docs/providers/`
- Server mode: `https://opencode.ai/docs/server/`

Detailed docs in this repo:

- `docs/ubuntu-cloudflare-desktop-setup.md`
- `docs/session-change-map.md`

## Installation Paths

There are two practical install paths documented here.

### Path A: Purchased Domain + Cloudflare DNS

This is the recommended path.

Use this when you want a stable hostname like:

- `https://mcp.example.com/mcp`

Why this is better:

- stable OAuth issuer URL
- stable connector configuration
- better long-term reliability
- easier to run multiple gateways like `mcp1`, `mcp2`, `mcp3`

### Path B: Free Cloudflare `trycloudflare` Tunnel

This is the free, no-personal-domain path.

Use this when you want to experiment without buying or wiring a domain.

Why this is worse:

- hostname is temporary
- hostname can change on reconnect
- OAuth clients may break when the issuer URL changes
- not ideal for durable Claude or ChatGPT connectors

## Quick Start

### 1. Install Ubuntu packages

```bash
sudo apt update
sudo apt install -y curl git python3 python3-pip python3-venv
```

### 2. Install and configure OpenCode

```bash
curl -fsSL https://opencode.ai/install | bash
```

Then configure a provider and start OpenCode locally:

```bash
opencode serve --hostname 127.0.0.1 --port 9999
```

### 3. Clone this repo and install dependencies

```bash
git clone https://github.com/gjabdelnoor/opencode-mcp-gateway-cloudflare-desktop.git
cd opencode-mcp-gateway-cloudflare-desktop
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Create `.env`

```bash
cp .env.example .env
```

Example:

```bash
MCP_AUTH_TOKEN=replace-with-a-long-random-secret
MCP_CLIENT_ID=opencode-mcp-gateway
MCP_ALLOWED_CLIENT_IDS=opencode-mcp-gateway
PUBLIC_BASE_URL=https://mcp.example.com
DEFAULT_WORKSPACE_DIR="/home/YOUR_USER/AI Projects"
OPENCODE_HOST=127.0.0.1
OPENCODE_PORT=9999
GATEWAY_PORT=3001
ENABLE_RAW_BASH=true
DEFAULT_PLANNING_MODEL=opencode/minimax-m2.5-free
DEFAULT_BUILDING_MODEL=openai/gpt-5.4-mini
```

### 5. Choose a tunnel path

#### Purchased domain path

Install `cloudflared`:

```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb
```

Authenticate and create the tunnel:

```bash
cloudflared tunnel login
cloudflared tunnel create opencode-mcp-gateway
cloudflared tunnel route dns opencode-mcp-gateway mcp.example.com
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: /home/YOUR_USER/.cloudflared/YOUR_TUNNEL_ID.json

ingress:
  - hostname: mcp.example.com
    service: http://127.0.0.1:3001
    originRequest:
      httpHostHeader: mcp.example.com
  - service: http_status:404
```

Set:

- `PUBLIC_BASE_URL=https://mcp.example.com`

Run the tunnel:

```bash
cloudflared tunnel run opencode-mcp-gateway
```

#### Free `trycloudflare` path

Install `cloudflared` the same way, then run:

```bash
cloudflared tunnel --url http://127.0.0.1:3001
```

That gives you a temporary `https://...trycloudflare.com` URL.

Use that URL as:

- `PUBLIC_BASE_URL=https://YOUR-TEMP-HOST.trycloudflare.com`

Important caveat:

- this path is still considered experimental here
- if the hostname changes, you will usually need to recreate the connector in Claude or ChatGPT

### 6. Start the gateway

```bash
source .venv/bin/activate
python main.py
```

## Verify The Deployment

Check:

```bash
curl https://mcp.example.com/.well-known/oauth-authorization-server
curl https://mcp.example.com/.well-known/oauth-authorization-server/mcp
curl https://mcp.example.com/.well-known/oauth-protected-resource
curl -D - -o /dev/null https://mcp.example.com/mcp
```

You want:

- OAuth issuer: `https://mcp.example.com`
- token endpoint: `https://mcp.example.com/oauth/token`
- protected resource: `https://mcp.example.com/mcp`
- unauthorized `/mcp` returns `401` with `WWW-Authenticate` and `resource_metadata`

## Connect ChatGPT Or Claude

MCP server URL:

```text
https://mcp.example.com/mcp
```

OAuth discovery URLs:

- Claude: `https://mcp.example.com/.well-known/oauth-authorization-server`
- ChatGPT: `https://mcp.example.com/.well-known/oauth-authorization-server/mcp`

Manual OAuth values when needed:

- OAuth Client ID: `opencode-mcp-gateway`
- OAuth Client Secret: your `MCP_AUTH_TOKEN`
- Token auth method: `client_secret_post`
- Scope: `mcp`

## Multiple Concurrent Agents

If you want multiple chatbot-controlled agents at once, run multiple gateway processes.

Recommended layout:

- `mcp1.example.com -> localhost:3001`
- `mcp2.example.com -> localhost:3002`
- `mcp3.example.com -> localhost:3003`
- `mcp4.example.com -> localhost:3004`
- `mcp5.example.com -> localhost:3005`
- `mcp6.example.com -> localhost:3006`

Each instance should have its own:

- `PUBLIC_BASE_URL`
- `GATEWAY_PORT`
- `MCP_AUTH_TOKEN`
- `MCP_CLIENT_ID`

## Configuration

| Variable | Description |
|---|---|
| `MCP_AUTH_TOKEN` | Bearer secret for OAuth token exchange and MCP access |
| `MCP_CLIENT_ID` | Main OAuth client ID accepted by the gateway |
| `MCP_ALLOWED_CLIENT_IDS` | Optional comma-separated allowlist of additional client IDs |
| `PUBLIC_BASE_URL` | External HTTPS base URL advertised in OAuth metadata |
| `DEFAULT_WORKSPACE_DIR` | Default project root for new sessions and PTYs |
| `OPENCODE_HOST` | OpenCode origin host |
| `OPENCODE_PORT` | OpenCode origin port |
| `GATEWAY_PORT` | Gateway listen port |
| `ENABLE_RAW_BASH` | Enables direct `bash` and `bash_exec` tools |
| `DEFAULT_PLANNING_MODEL` | Optional fallback model for planning-mode sessions |
| `DEFAULT_BUILDING_MODEL` | Optional fallback model for building-mode sessions |
| `BLOCKED_SESSION_MODELS` | Optional comma-separated models to reject even if OpenCode advertises them |

## Troubleshooting

### ChatGPT says the server URL is invalid

Use the full MCP URL:

```text
https://mcp.example.com/mcp
```

### Claude reaches login but the connector still fails

Check:

- `PUBLIC_BASE_URL` is correct
- protected resource metadata returns `https://mcp.example.com/mcp`
- `GET /mcp` without auth returns `401` with `WWW-Authenticate`
- the OAuth client secret exactly matches `MCP_AUTH_TOKEN`

### `session_create` or `send_message` looks stalled

Check:

```bash
curl http://127.0.0.1:9999/session/status
```

If OpenCode is retrying an unsupported model, set or adjust:

```bash
DEFAULT_PLANNING_MODEL=opencode/minimax-m2.5-free
DEFAULT_BUILDING_MODEL=openai/gpt-5.4-mini
```

### `switch_model` rejects a model you thought should work

The gateway now validates against OpenCode’s live model catalog.

It will reject:

- anything not currently exposed by OpenCode
- I may have hardcoded it to reject the two known-bad models on my minimax plan... oops:
  - `minimax-coding-plan/MiniMax-M2.5-highspeed`
  - `minimax-coding-plan/MiniMax-M2.7-highspeed`

### Sessions are starting in the wrong folder

Set:

```bash
DEFAULT_WORKSPACE_DIR="/home/YOUR_USER/AI Projects"
```

This repo now defaults new sessions and PTYs to that workspace if you do not pass an explicit directory.

### Several bots interfere with each other

Use separate gateway instances on separate hostnames and ports.

## Full Docs

- `docs/ubuntu-cloudflare-desktop-setup.md`
- `docs/session-change-map.md`

## Questions Or Security Concerns

If you have questions, comments, setup issues, or serious security concerns, contact `@isnotgabe` on Discord.
