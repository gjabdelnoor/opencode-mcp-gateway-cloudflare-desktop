#!/usr/bin/env python3
"""
OpenCode MCP Gateway - Exposes OpenCode as a remote MCP server for Claude Code.
Uses FastMCP with streamable-http transport.
"""

import os
import asyncio
import secrets
import hashlib
import base64

import structlog
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

from opencode_client import OpenCodeClient
from session_manager import SessionManager
from pty_manager import PtyManager

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", secrets.token_hex(32))

OPENCODE_HOST = os.environ.get("OPENCODE_HOST", "localhost")
OPENCODE_PORT = int(os.environ.get("OPENCODE_PORT", "9999"))
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "3001"))

SERVER_NAME = "opencode-mcp-gateway"

oc_client: OpenCodeClient = None
session_mgr: SessionManager = None
pty_mgr: PtyManager = None

auth_codes = {}


def create_fastmcp() -> FastMCP:
    """Create and configure the FastMCP server."""
    global oc_client, session_mgr, pty_mgr

    mcp = FastMCP(
        name=SERVER_NAME,
        instructions="OpenCode MCP Gateway - Access OpenCode sessions and tools",
    )

    @mcp.tool()
    def list_sessions(cursor: str = None, limit: int = 10) -> dict:
        """List OpenCode sessions with pagination.

        Returns all known sessions (up to limit) with recent message previews.
        Use cursor from previous response to get next page.

        Args:
            cursor: Cursor from previous response to get next page (optional)
            limit: Maximum sessions to return (default 10, max 50)
        """
        import asyncio
        limit = min(limit, 50)
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.list_sessions(cursor=cursor, limit=limit)
        )

    @mcp.tool()
    def session_create(
        initial_message: str,
        title: str = None,
        directory: str = None,
        mode: str = "planning",
        auto_accept: bool = False
    ) -> dict:
        """Create a new OpenCode session with mandatory initial message.

        Creates a new session in planning mode by default, sends the initial message,
        and returns the response. The session will be in planning mode until you
        explicitly switch it to building mode.

        Args:
            initial_message: REQUIRED - First message to send to the session
            title: Optional session title
            directory: Optional working directory
            mode: 'planning' (default) or 'building' - mode for the new session
            auto_accept: If True, sets permissions to allow all (no permission prompts)
        """
        import asyncio
        permissions = None
        if auto_accept:
            permissions = [{"permission": "*", "pattern": "*", "action": "allow"}]
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.create_session(
                initial_message=initial_message,
                title=title,
                directory=directory,
                mode=mode,
                permissions=permissions
            )
        )

    @mcp.tool()
    def session_get(session_id: str) -> dict:
        """Get session details."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(session_mgr.get_session(session_id))

    @mcp.tool()
    def session_delete(session_id: str) -> dict:
        """Delete a session."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(session_mgr.delete_session(session_id))

    @mcp.tool()
    def session_fork(session_id: str) -> dict:
        """Fork a session."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(session_mgr.fork_session(session_id))

    @mcp.tool()
    def send_message(session_id: str, prompt: str) -> dict:
        """Send a message to a session with timeout handling.

        Sends the message and waits for OpenCode's response. If the response
        takes too long (approaching 50s timeout), returns partial result with
        still_active=True. Use read_session_logs to get full output.

        Args:
            session_id: The session ID to send the message to
            prompt: The message text
        """
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.send_message(session_id, prompt)
        )

    @mcp.tool()
    def message_abort(session_id: str) -> dict:
        """Abort ongoing generation."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(session_mgr.abort_message(session_id))

    @mcp.tool()
    def bash_create(cwd: str = None) -> dict:
        """Create a PTY terminal."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(pty_mgr.create_pty(cwd=cwd, owner="claude"))

    @mcp.tool()
    def bash_read(pty_id: str) -> str:
        """Read PTY output."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(pty_mgr.read_output(pty_id))

    @mcp.tool()
    def bash_resize(pty_id: str, cols: int, rows: int) -> dict:
        """Resize PTY."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(pty_mgr.resize_pty(pty_id, cols, rows))

    @mcp.tool()
    def bash_close(pty_id: str) -> dict:
        """Close PTY."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(pty_mgr.close_pty(pty_id))

    @mcp.tool()
    def status() -> dict:
        """Get gateway status."""
        sessions = session_mgr.get_all_session_ids()
        ptys = pty_mgr.get_claude_ptys()
        return {
            "status": "healthy",
            "total_sessions": len(sessions),
            "claude_sessions": len(session_mgr.get_claude_session_ids()),
            "claude_ptys": ptys,
        }

    @mcp.tool()
    def read_session_logs(session_id: str, mode: str = "summary") -> dict:
        """Read session logs (non-blocking).

        Read a session's message history without waiting. Use this to check
        session output divorced from wait_for_session monitoring.

        Args:
            session_id: The session ID
            mode: "summary" (last 3 messages, default) or "full" (all messages)
        """
        import asyncio
        if mode not in ("summary", "full"):
            mode = "summary"
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.read_session_logs(session_id, mode=mode)
        )

    @mcp.tool()
    def switch_session(session_id: str) -> dict:
        """Switch to a different session."""
        return session_mgr.set_active_session(session_id)

    @mcp.tool()
    def switch_model(session_id: str, model: str) -> dict:
        """Set the model for a session (e.g., 'openai/gpt-4o', 'anthropic/claude-3-5-sonnet')."""
        return session_mgr.set_session_model(session_id, model)

    @mcp.tool()
    def switch_mode_and_send(session_id: str, mode: str, message: str) -> dict:
        """Switch a session to a different mode AND send a message in one call.

        This is the primary way to transition from planning to building mode.
        Switching mode and sending a message together ensures the agent
        understands the context of why the mode changed.

        Args:
            session_id: The session ID
            mode: Target mode - 'planning' or 'building'
            message: Message to send after switching mode (e.g., "Now build this plan")
        """
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.switch_mode_and_send(session_id, mode, message)
        )

    @mcp.tool()
    def get_session_mode(session_id: str) -> dict:
        """Get the current mode of a session (planning or building)."""
        mode = session_mgr.get_session_mode(session_id)
        return {"session_id": session_id, "mode": mode}

    @mcp.tool()
    def get_active_session() -> dict:
        """Get the currently active session."""
        active_id = session_mgr.get_active_session()
        return {"active_session_id": active_id}

    @mcp.tool()
    def set_permissions(session_id: str, permissions: list) -> dict:
        """Set permissions for a session.

        Args:
            session_id: The session ID
            permissions: List of permission dicts, e.g., [{"permission": "*", "pattern": "*", "action": "allow"}]
                       Valid actions: "allow", "deny", "ask"
        """
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.set_session_permissions(session_id, permissions)
        )

    @mcp.tool()
    def auto_accept_permissions(session_id: str) -> dict:
        """Enable auto-accept (allow all) permissions for a session.

        This removes all permission prompts for the session.
        """
        permissions = [{"permission": "*", "pattern": "*", "action": "allow"}]
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.set_session_permissions(session_id, permissions)
        )

    @mcp.tool()
    def wait_for_session(session_id: str, duration: int = 50) -> dict:
        """Wait for a session and collect activity.

        Monitors a session for the specified duration, collecting tool calls,
        outputs, and internal reasoning. Returns a summary of activity suitable
        for deciding if the agent needs steering/correction.

        Minimum duration is 30 seconds. If session still active near timeout,
        returns partial results with still_active=True and flavor text suggesting
        to use read_session_logs.

        Args:
            session_id: The session ID to monitor
            duration: Seconds to wait (minimum 30, default 50)
        """
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            session_mgr.wait_for_session(session_id, duration)
        )

    return mcp


async def handle_health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({
        "status": "healthy",
        "server": SERVER_NAME,
        "auth_required": True
    })


async def handle_oauth_authorize(request: Request) -> HTMLResponse:
    """OAuth authorization page."""
    params = dict(request.query_params)
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")
    scope = params.get("scope", "mcp")

    if not secrets.compare_digest(client_id, "opencode-mcp-gateway"):
        return HTMLResponse("<h1>Invalid client_id</h1>", status_code=400)

    code = secrets.token_hex(32)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Authorize Claude Code</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; }}
            .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 30px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            h1 {{ font-size: 24px; margin-bottom: 10px; }}
            p {{ color: #666; margin-bottom: 20px; }}
            .client {{ font-weight: bold; color: #333; }}
            .scopes {{ background: #f5f5f5; padding: 10px; border-radius: 4px; margin: 15px 0; font-size: 14px; }}
            .buttons {{ display: flex; gap: 10px; margin-top: 25px; }}
            button {{ flex: 1; padding: 12px 20px; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
            .approve {{ background: #d73a49; color: white; }}
            .deny {{ background: #f6f7f8; color: #333; border: 1px solid #ddd; }}
            button:hover {{ opacity: 0.9; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Authorize Claude Code</h1>
            <p><span class="client">Claude Code</span> is requesting access to your OpenCode server.</p>
            <div class="scopes">
                <strong>Scopes:</strong> {scope}
            </div>
            <p style="font-size: 14px; color: #888;">Client ID: {client_id}</p>
            <div class="buttons">
                <form method="post" action="/oauth/authorize" style="flex:1;">
                    <input type="hidden" name="client_id" value="{client_id}">
                    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
                    <input type="hidden" name="state" value="{state}">
                    <input type="hidden" name="code" value="{code}">
                    <input type="hidden" name="code_challenge" value="{code_challenge}">
                    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
                    <input type="hidden" name="scope" value="{scope}">
                    <button type="submit" name="action" value="approve" class="approve">Authorize</button>
                </form>
                <form method="post" action="/oauth/authorize" style="flex:1;">
                    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
                    <input type="hidden" name="state" value="{state}">
                    <button type="submit" name="action" value="deny" class="deny">Deny</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html)


async def handle_oauth_authorize_post(request: Request) -> RedirectResponse:
    """Handle authorization approval/denial."""
    form = await request.form()
    action = form.get("action", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")

    if action != "approve":
        import urllib.parse
        params = urllib.parse.urlencode({"error": "access_denied", "state": state})
        return RedirectResponse(f"{redirect_uri}?{params}", status_code=302)

    code = form.get("code", "")
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "S256")
    scope = form.get("scope", "mcp")

    auth_codes[code] = {
        "client_id": "opencode-mcp-gateway",
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "expires": asyncio.get_event_loop().time() + 300
    }

    import urllib.parse
    params = urllib.parse.urlencode({"code": code, "state": state})
    return RedirectResponse(f"{redirect_uri}?{params}", status_code=302)


async def handle_oauth_token(request: Request) -> JSONResponse:
    """OAuth token endpoint."""
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
        elif "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            body = {k: str(v) for k, v in form.items()}
        else:
            body = await request.json()
        logger.info("token_request", body=body, headers=dict(request.headers))
        grant_type = str(body.get("grant_type", ""))
        code = str(body.get("code", ""))
        code_verifier = str(body.get("code_verifier", ""))

        if grant_type == "authorization_code" and code:
            auth_info = auth_codes.pop(code, None)
            if not auth_info:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            if asyncio.get_event_loop().time() > auth_info["expires"]:
                return JSONResponse({"error": "code_expired"}, status_code=400)

            if auth_info["code_challenge_method"] == "S256":
                verifier_hash = hashlib.sha256(code_verifier.encode()).digest()
                expected_challenge = base64.urlsafe_b64encode(verifier_hash).decode().rstrip("=")
            else:
                expected_challenge = code_verifier

            if not secrets.compare_digest(expected_challenge, auth_info["code_challenge"]):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            return JSONResponse({
                "access_token": AUTH_TOKEN,
                "token_type": "Bearer",
                "expires_in": 86400
            })

        client_id = body.get("client_id", "")
        client_secret = body.get("client_secret", "")

        if secrets.compare_digest(client_id, "opencode-mcp-gateway") and \
           secrets.compare_digest(client_secret, AUTH_TOKEN):
            return JSONResponse({
                "access_token": AUTH_TOKEN,
                "token_type": "Bearer",
                "expires_in": 86400
            })

        return JSONResponse({"error": "invalid_client"}, status_code=401)
    except Exception as e:
        logger.error("oauth_token_error", error=str(e), headers=dict(request.headers))
        return JSONResponse({"error": str(e)}, status_code=500)


class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if response.status_code in (307, 308):
            location = response.headers.get("location", "")
            if location.startswith("http://"):
                location = "https://" + location[7:]
                response.headers["location"] = location
        return response


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        oauth_paths = ["/authorize", "/oauth/", "/.well-known/"]
        mcp_oauth_paths = ["/mcp/authorize", "/mcp/oauth/", "/mcp/.well-known/"]
        if any(path.startswith(p) for p in oauth_paths + mcp_oauth_paths):
            return await call_next(request)
        
        if path.startswith("/mcp") and path != "/mcp/health" and not path.startswith("/mcp/"):
            auth_header = request.headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse({"error": "invalid_token", "error_description": "Missing or invalid Authorization header"}, status_code=401)
            token = auth_header[7:]
            if not secrets.compare_digest(token, AUTH_TOKEN):
                return JSONResponse({"error": "invalid_token"}, status_code=401)
        response = await call_next(request)
        return response


async def handle_oauth_discovery(request: Request) -> JSONResponse:
    """OAuth authorization server discovery endpoint."""
    path = request.url.path
    base_url = "https://mcp.homunculi.cloud"
    
    if path.endswith("/mcp"):
        base_url = "https://mcp.homunculi.cloud/mcp"
    
    return JSONResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": ["mcp", "openid", "profile", "email"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "mcp_client_id": "opencode-mcp-gateway",
    })


async def handle_protected_resource(request: Request) -> JSONResponse:
    """Protected resource metadata endpoint (RFC 9728)."""
    return JSONResponse({
        "resource": "https://mcp.homunculi.cloud",
        "authorization_servers": ["https://mcp.homunculi.cloud"],
        "scopes_supported": ["mcp", "openid", "profile", "email"],
        "bearer_methods_supported": ["header"],
    })


def main():
    """Main entry point."""
    global oc_client, session_mgr, pty_mgr

    logger.info("gateway_starting",
                opencode_host=OPENCODE_HOST,
                opencode_port=OPENCODE_PORT,
                gateway_port=GATEWAY_PORT)

    oc_client = OpenCodeClient(base_url=f"http://{OPENCODE_HOST}:{OPENCODE_PORT}")
    session_mgr = SessionManager(oc_client)
    pty_mgr = PtyManager(oc_client)

    mcp = create_fastmcp()
    mcp_app = mcp.http_app(path="/mcp")

    mcp_app.add_middleware(HTTPSRedirectMiddleware)
    mcp_app.add_middleware(BearerAuthMiddleware)
    mcp_app.add_route("/health", handle_health, methods=["GET"])
    mcp_app.add_route("/.well-known/oauth-authorization-server", handle_oauth_discovery, methods=["GET"])
    mcp_app.add_route("/.well-known/oauth-authorization-server/mcp", handle_oauth_discovery, methods=["GET"])
    mcp_app.add_route("/.well-known/oauth-protected-resource", handle_protected_resource, methods=["GET"])
    mcp_app.add_route("/.well-known/oauth-protected-resource/mcp", handle_protected_resource, methods=["GET"])
    mcp_app.add_route("/authorize", handle_oauth_authorize, methods=["GET"])
    mcp_app.add_route("/oauth/authorize", handle_oauth_authorize, methods=["GET"])
    mcp_app.add_route("/oauth/authorize", handle_oauth_authorize_post, methods=["POST"])
    mcp_app.add_route("/oauth/token", handle_oauth_token, methods=["POST"])
    mcp_app.add_route("/mcp/authorize", handle_oauth_authorize, methods=["GET"])
    mcp_app.add_route("/mcp/oauth/authorize", handle_oauth_authorize, methods=["GET"])
    mcp_app.add_route("/mcp/oauth/authorize", handle_oauth_authorize_post, methods=["POST"])
    mcp_app.add_route("/mcp/oauth/token", handle_oauth_token, methods=["POST"])

    uvicorn.run(
        mcp_app,
        host="0.0.0.0",
        port=GATEWAY_PORT,
        log_level="info",
        access_log=False
    )


if __name__ == "__main__":
    main()
