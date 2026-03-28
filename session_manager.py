import asyncio
from typing import Optional
from datetime import datetime
import structlog
from opencode_client import OpenCodeClient, Session

logger = structlog.get_logger()


class SessionInfo:
    def __init__(self, session_id: str, title: str, owner: str, created_at: datetime):
        self.id = session_id
        self.title = title
        self.owner = owner  # "user" or "claude"
        self.created_at = created_at
        self.last_used = created_at
        self.client: Optional[OpenCodeClient] = None

    def touch(self):
        self.last_used = datetime.now()


class SessionManager:
    def __init__(self, oc_client: OpenCodeClient):
        self.oc = oc_client
        self.sessions: dict[str, SessionInfo] = {}
        self.user_session_ids: set[str] = set()
        self.claude_session_ids: set[str] = set()
        self.active_session_id: Optional[str] = None
        self.session_models: dict[str, str] = {}
        self.session_modes: dict[str, str] = {}  # "planning" or "building"
        self._lock = asyncio.Lock()

    async def refresh_user_sessions(self):
        """Load user's existing sessions from OpenCode."""
        async with self._lock:
            try:
                sessions = await self.oc.list_sessions()
                self.user_session_ids = {s.id for s in sessions}
                logger.info("refreshed_user_sessions", count=len(sessions))
            except Exception as e:
                logger.error("failed_to_refresh_user_sessions", error=str(e))

    def get_all_session_ids(self) -> list[str]:
        """Return all known session IDs."""
        return list(self.user_session_ids) + list(self.claude_session_ids)

    def get_claude_session_ids(self) -> list[str]:
        """Return session IDs owned by Claude."""
        return list(self.claude_session_ids)

    async def create_session(self, title: Optional[str] = None, directory: Optional[str] = None, owner: str = "claude", permissions: Optional[list] = None) -> dict:
        """Create a new session. Returns session info dict."""
        async with self._lock:
            result = await self.oc.create_session(title=title, directory=directory, permissions=permissions)
            session_id = result.get("id")
            if session_id:
                info = SessionInfo(
                    session_id=session_id,
                    title=title or "Untitled",
                    owner=owner,
                    created_at=datetime.now()
                )
                self.sessions[session_id] = info
                if owner == "claude":
                    self.claude_session_ids.add(session_id)
                logger.info("created_session", session_id=session_id, owner=owner)
            return result

    async def delete_session(self, session_id: str) -> dict:
        """Delete a session."""
        async with self._lock:
            result = await self.oc.delete_session(session_id)
            if session_id in self.sessions:
                del self.sessions[session_id]
            self.claude_session_ids.discard(session_id)
            self.user_session_ids.discard(session_id)
            logger.info("deleted_session", session_id=session_id)
            return result

    async def fork_session(self, session_id: str) -> dict:
        """Fork an existing session."""
        async with self._lock:
            result = await self.oc.fork_session(session_id)
            new_id = result.get("id")
            if new_id:
                info = SessionInfo(
                    session_id=new_id,
                    title=f"Fork of {session_id}",
                    owner="claude",
                    created_at=datetime.now()
                )
                self.sessions[new_id] = info
                self.claude_session_ids.add(new_id)
                logger.info("forked_session", original=session_id, forked=new_id)
            return result

    async def send_message(self, session_id: str, prompt: str, stream: bool = True, model: Optional[str] = None):
        """Send a message to a session.
        
        Args:
            session_id: The session ID to send the message to
            prompt: The message text
            stream: Whether to stream the response
            model: Optional model override. If not provided, uses the session's configured model.
        """
        if model is None:
            model = self.session_models.get(session_id)
        if stream:
            return self.oc.stream_message(session_id, prompt, model=model)
        else:
            return await self.oc.send_message(session_id, prompt, model=model)

    async def abort_message(self, session_id: str) -> dict:
        """Abort ongoing message generation."""
        return await self.oc.abort_message(session_id)

    async def get_session(self, session_id: str) -> dict:
        """Get full session state."""
        return await self.oc.get_session(session_id)

    async def list_sessions(self) -> list[dict]:
        """List all sessions with metadata."""
        await self.refresh_user_sessions()
        all_ids = self.get_all_session_ids()
        result = []
        for sid in all_ids:
            try:
                sess = await self.oc.get_session(sid)
                owner = "claude" if sid in self.claude_session_ids else "user"
                model = self.session_models.get(sid)
                result.append({
                    "id": sid,
                    "title": sess.get("title", "Untitled"),
                    "owner": owner,
                    "directory": sess.get("directory"),
                    "created": sess.get("time", {}).get("created"),
                    "updated": sess.get("time", {}).get("updated"),
                    "model": model,
                    "is_active": sid == self.active_session_id,
                })
            except Exception as e:
                logger.warning("failed_to_get_session", session_id=sid, error=str(e))
                continue
        return result

    def set_active_session(self, session_id: str) -> dict:
        """Set the active session for Claude."""
        if session_id in self.user_session_ids or session_id in self.claude_session_ids:
            self.active_session_id = session_id
            logger.info("set_active_session", session_id=session_id)
            return {"success": True, "active_session_id": session_id}
        return {"success": False, "error": "Session not found"}

    def get_active_session(self) -> Optional[str]:
        """Get the active session ID."""
        return self.active_session_id

    def set_session_model(self, session_id: str, model: str) -> dict:
        """Set the model for a session."""
        self.session_models[session_id] = model
        logger.info("set_session_model", session_id=session_id, model=model)
        return {"success": True, "session_id": session_id, "model": model}

    def get_session_model(self, session_id: str) -> Optional[str]:
        """Get the model for a session."""
        return self.session_models.get(session_id)

    def set_session_mode(self, session_id: str, mode: str) -> dict:
        """Set the mode for a session (planning or building).
        
        Args:
            session_id: The session ID
            mode: Either "planning" or "building"
        """
        if mode not in ("planning", "building"):
            return {"success": False, "error": "Mode must be 'planning' or 'building'"}
        
        if session_id not in self.sessions and session_id not in self.user_session_ids:
            return {"success": False, "error": "Session not found"}
        
        self.session_modes[session_id] = mode
        logger.info("set_session_mode", session_id=session_id, mode=mode)
        return {"success": True, "session_id": session_id, "mode": mode}

    def get_session_mode(self, session_id: str) -> Optional[str]:
        """Get the mode for a session."""
        return self.session_modes.get(session_id)

    async def set_session_permissions(self, session_id: str, permissions: list) -> dict:
        """Set permissions for a session.
        
        Args:
            session_id: The session ID
            permissions: List of permission dicts, e.g., [{"permission": "*", "pattern": "*", "action": "allow"}]
        """
        if session_id not in self.sessions:
            return {"success": False, "error": "Session not found in manager"}
        
        try:
            result = await self.oc.update_session(session_id, permission=permissions)
            logger.info("set_session_permissions", session_id=session_id, permissions=permissions)
            return {"success": True, "session_id": session_id, "permissions": permissions}
        except Exception as e:
            logger.error("set_session_permissions_error", session_id=session_id, error=str(e))
            return {"success": False, "error": str(e)}

    async def wait_for_session(self, session_id: str, duration: int = 50) -> dict:
        """Wait for a session and collect activity.
        
        Monitors a session for the specified duration (default 50 seconds),
        collecting tool calls and their outputs. Returns a summary of activity.
        
        Args:
            session_id: The session ID to monitor
            duration: Seconds to wait (default 50)
            
        Returns:
            dict with activity summary including tool calls, outputs, and reasoning
        """
        import time
        start_time = time.time()
        activity = {
            "tool_calls": [],
            "outputs": [],
            "reasoning": [],
            "messages": [],
            "duration_seconds": duration,
        }
        
        last_message_count = 0
        check_interval = 2  # Check every 2 seconds
        
        while time.time() - start_time < duration:
            try:
                session_state = await self.oc.get_session(session_id)
                
                current_messages = session_state.get("messages", [])
                if len(current_messages) > last_message_count:
                    for msg in current_messages[last_message_count:]:
                        activity["messages"].append({
                            "role": msg.get("role"),
                            "content": msg.get("content", "")[:200] if msg.get("content") else "",
                        })
                        
                        if msg.get("role") == "assistant":
                            parts = msg.get("parts", [])
                            for part in parts:
                                if part.get("type") == "tool_use":
                                    tool_name = part.get("name", "unknown")
                                    tool_input = part.get("input", {})
                                    activity["tool_calls"].append({
                                        "tool": tool_name,
                                        "input": str(tool_input)[:100],
                                    })
                                elif part.get("type") == "text":
                                    text = part.get("text", "")
                                    if len(text) > 10:
                                        activity["reasoning"].append(text[:500])
                    
                    last_message_count = len(current_messages)
                
                await asyncio.sleep(check_interval)
            except Exception as e:
                logger.error("wait_for_session_error", session_id=session_id, error=str(e))
                activity["error"] = str(e)
                break
        
        activity["elapsed_seconds"] = int(time.time() - start_time)
        
        summary_parts = []
        if activity["tool_calls"]:
            summary_parts.append(f"Tools called ({len(activity['tool_calls'])}): ")
            for tc in activity["tool_calls"][:5]:
                summary_parts.append(f"  - {tc['tool']}: {tc['input'][:60]}...")
        
        if activity["reasoning"]:
            summary_parts.append(f"\nInternal reasoning ({len(activity['reasoning'])} entries):")
            for r in activity["reasoning"][:3]:
                summary_parts.append(f"  {r[:100]}...")
        
        activity["summary"] = "\n".join(summary_parts) if summary_parts else "No significant activity"
        
        return activity