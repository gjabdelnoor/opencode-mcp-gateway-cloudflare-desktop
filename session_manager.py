import asyncio
import time
from typing import Optional, Literal
from datetime import datetime
import structlog
from opencode_client import OpenCodeClient, Session

logger = structlog.get_logger()

TOOL_TIMEOUT = 50
NEAR_TIMEOUT_THRESHOLD = 45
MIN_WAIT_DURATION = 30


class SessionInfo:
    def __init__(self, session_id: str, title: str, owner: str, created_at: datetime):
        self.id = session_id
        self.title = title
        self.owner = owner
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
        self.session_modes: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def refresh_user_sessions(self):
        async with self._lock:
            try:
                sessions = await self.oc.list_sessions()
                self.user_session_ids = {s.id for s in sessions}
                logger.info("refreshed_user_sessions", count=len(sessions))
            except Exception as e:
                logger.error("failed_to_refresh_user_sessions", error=str(e))

    def get_all_session_ids(self) -> list[str]:
        return list(self.user_session_ids) + list(self.claude_session_ids)

    def get_claude_session_ids(self) -> list[str]:
        return list(self.claude_session_ids)

    async def create_session(
        self,
        initial_message: str,
        title: Optional[str] = None,
        directory: Optional[str] = None,
        owner: str = "claude",
        mode: str = "planning",
        permissions: Optional[list] = None
    ) -> dict:
        async with self._lock:
            result = await self.oc.create_session(
                title=title,
                directory=directory,
                permissions=permissions
            )
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
                self.session_modes[session_id] = mode
                logger.info("created_session", session_id=session_id, owner=owner, mode=mode)

                if initial_message:
                    send_result = await self._send_message_with_timeout(
                        session_id, initial_message
                    )
                    result["initial_response"] = send_result

            return result

    async def delete_session(self, session_id: str) -> dict:
        async with self._lock:
            result = await self.oc.delete_session(session_id)
            if session_id in self.sessions:
                del self.sessions[session_id]
            self.claude_session_ids.discard(session_id)
            self.user_session_ids.discard(session_id)
            logger.info("deleted_session", session_id=session_id)
            return result

    async def fork_session(self, session_id: str) -> dict:
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

    async def _send_message_with_timeout(
        self,
        session_id: str,
        prompt: str,
        model: Optional[str] = None,
        timeout: int = TOOL_TIMEOUT
    ) -> dict:
        if model is None:
            model = self.session_models.get(session_id)

        collected = {
            "parts": [],
            "tool_calls": [],
            "reasoning": [],
            "final_text": "",
            "completed": False,
        }

        start_time = time.time()
        near_timeout_returned = False

        try:
            stream = self.oc.stream_message(session_id, prompt, model=model)
            async for event in stream:
                elapsed = time.time() - start_time

                event_type = event.get("type", "")
                if event_type == "content_block":
                    content = event.get("content", {})
                    if content.get("type") == "text":
                        collected["final_text"] += content.get("text", "")
                    elif content.get("type") == "tool_use":
                        tool_name = content.get("name", "unknown")
                        tool_input = content.get("input", {})
                        collected["tool_calls"].append({
                            "tool": tool_name,
                            "input": tool_input,
                        })
                        collected["parts"].append({
                            "type": "tool_use",
                            "tool": tool_name,
                            "input": str(tool_input)[:200],
                        })
                elif event_type == "text":
                    collected["final_text"] += event.get("text", "")
                elif event_type == "done":
                    collected["completed"] = True
                    break

                if elapsed >= NEAR_TIMEOUT_THRESHOLD and not collected["completed"]:
                    near_timeout_returned = True
                    collected["reasoning"].append(
                        f"Response still in progress after {int(elapsed)} seconds. "
                        "Session is still active."
                    )
                    break

        except Exception as e:
            logger.error("send_message_error", session_id=session_id, error=str(e))
            collected["error"] = str(e)

        elapsed = time.time() - start_time
        collected["elapsed_seconds"] = int(elapsed)

        if near_timeout_returned or (elapsed >= NEAR_TIMEOUT_THRESHOLD and not collected["completed"]):
            return {
                "partial_result": {
                    "text": collected["final_text"][:1000] if collected["final_text"] else "",
                    "tool_calls": collected["tool_calls"][:5],
                    "message": f"Response still in progress after {int(elapsed)} seconds. "
                              f"Use *read_session_logs* to check full output, or "
                              f"*wait_for_session* to continue monitoring."
                },
                "reasoning_so_far": collected["reasoning"],
                "still_active": True,
                "elapsed_seconds": int(elapsed),
            }

        return {
            "text": collected["final_text"],
            "tool_calls": collected["tool_calls"],
            "completed": True,
            "elapsed_seconds": int(elapsed),
        }

    async def send_message(self, session_id: str, prompt: str, model: Optional[str] = None) -> dict:
        return await self._send_message_with_timeout(session_id, prompt, model=model)

    async def send_message_stream(self, session_id: str, prompt: str, stream: bool = True, model: Optional[str] = None):
        if model is None:
            model = self.session_models.get(session_id)
        if stream:
            return self.oc.stream_message(session_id, prompt, model=model)
        else:
            return await self.oc.send_message(session_id, prompt, model=model)

    async def abort_message(self, session_id: str) -> dict:
        return await self.oc.abort_message(session_id)

    async def get_session(self, session_id: str) -> dict:
        return await self.oc.get_session(session_id)

    async def list_sessions(self, cursor: Optional[str] = None, limit: int = 10) -> dict:
        await self.refresh_user_sessions()
        all_ids = self.get_all_session_ids()

        if cursor:
            try:
                start_idx = all_ids.index(cursor) + 1
                all_ids = all_ids[start_idx:]
            except ValueError:
                pass

        result = []
        last_id = None
        for sid in all_ids[:limit]:
            try:
                sess = await self.oc.get_session(sid)
                owner = "claude" if sid in self.claude_session_ids else "user"
                mode = self.session_modes.get(sid, "planning")
                model = self.session_models.get(sid)

                messages = sess.get("messages", [])
                recent_messages = []
                for msg in messages[-3:]:
                    recent_messages.append({
                        "role": msg.get("role"),
                        "content": msg.get("content", "")[:300] if msg.get("content") else "",
                        "parts_count": len(msg.get("parts", [])),
                    })

                result.append({
                    "id": sid,
                    "title": sess.get("title", "Untitled"),
                    "owner": owner,
                    "directory": sess.get("directory"),
                    "created": sess.get("time", {}).get("created"),
                    "updated": sess.get("time", {}).get("updated"),
                    "model": model,
                    "mode": mode,
                    "is_active": sid == self.active_session_id,
                    "recent_messages": recent_messages,
                })
                last_id = sid
            except Exception as e:
                logger.warning("failed_to_get_session", session_id=sid, error=str(e))
                continue

        next_cursor = last_id if len(all_ids) > limit else None

        return {
            "sessions": result,
            "next_cursor": next_cursor,
            "total": len(all_ids),
        }

    async def read_session_logs(self, session_id: str, mode: Literal["summary", "full"] = "summary") -> dict:
        try:
            sess = await self.oc.get_session(session_id)
            messages = sess.get("messages", [])

            if mode == "summary":
                messages = messages[-3:]

            parsed_messages = []
            for msg in messages:
                parts = msg.get("parts", [])
                parsed_parts = []

                for part in parts:
                    part_type = part.get("type", "")
                    if part_type == "text":
                        parsed_parts.append({
                            "type": "text",
                            "text": part.get("text", "")[:500],
                        })
                    elif part_type == "tool_use":
                        parsed_parts.append({
                            "type": "tool_use",
                            "tool": part.get("name", "unknown"),
                            "input": str(part.get("input", {}))[:200],
                        })
                    elif part_type == "tool_result":
                        parsed_parts.append({
                            "type": "tool_result",
                            "content": str(part.get("content", ""))[:200],
                        })

                parsed_messages.append({
                    "id": msg.get("id", ""),
                    "role": msg.get("role", ""),
                    "content": msg.get("content", "")[:500] if msg.get("content") else None,
                    "parts": parsed_parts,
                })

            return {
                "session_id": session_id,
                "mode": mode,
                "messages": parsed_messages,
                "total_messages": len(sess.get("messages", [])),
            }
        except Exception as e:
            logger.error("read_session_logs_error", session_id=session_id, error=str(e))
            return {"error": str(e), "session_id": session_id}

    def set_active_session(self, session_id: str) -> dict:
        if session_id in self.user_session_ids or session_id in self.claude_session_ids:
            self.active_session_id = session_id
            logger.info("set_active_session", session_id=session_id)
            return {"success": True, "active_session_id": session_id}
        return {"success": False, "error": "Session not found"}

    def get_active_session(self) -> Optional[str]:
        return self.active_session_id

    def set_session_model(self, session_id: str, model: str) -> dict:
        self.session_models[session_id] = model
        logger.info("set_session_model", session_id=session_id, model=model)
        return {"success": True, "session_id": session_id, "model": model}

    def get_session_model(self, session_id: str) -> Optional[str]:
        return self.session_models.get(session_id)

    def set_session_mode(self, session_id: str, mode: str) -> dict:
        if mode not in ("planning", "building"):
            return {"success": False, "error": "Mode must be 'planning' or 'building'"}

        if session_id not in self.sessions and session_id not in self.user_session_ids:
            return {"success": False, "error": "Session not found"}

        self.session_modes[session_id] = mode
        logger.info("set_session_mode", session_id=session_id, mode=mode)
        return {"success": True, "session_id": session_id, "mode": mode}

    def get_session_mode(self, session_id: str) -> Optional[str]:
        return self.session_modes.get(session_id)

    async def switch_mode_and_send(
        self,
        session_id: str,
        mode: str,
        message: str
    ) -> dict:
        mode_result = self.set_session_mode(session_id, mode)
        if not mode_result.get("success"):
            return mode_result

        send_result = await self._send_message_with_timeout(session_id, message)
        send_result["mode_switched_to"] = mode

        return send_result

    async def set_session_permissions(self, session_id: str, permissions: list) -> dict:
        if session_id not in self.sessions:
            return {"success": False, "error": "Session not found in manager"}

        try:
            result = await self.oc.update_session(session_id, permission=permissions)
            logger.info("set_session_permissions", session_id=session_id, permissions=permissions)
            return {"success": True, "session_id": session_id, "permissions": permissions}
        except Exception as e:
            logger.error("set_session_permissions_error", session_id=session_id, error=str(e))
            return {"success": False, "error": str(e)}

    async def wait_for_session(
        self,
        session_id: str,
        duration: int = 50
    ) -> dict:
        duration = max(duration, MIN_WAIT_DURATION)

        start_time = time.time()
        activity = {
            "tool_calls": [],
            "outputs": [],
            "reasoning": [],
            "messages": [],
            "duration_seconds": duration,
            "session_id": session_id,
        }

        last_message_count = 0
        check_interval = 2
        near_timeout_returned = False

        while time.time() - start_time < duration:
            try:
                session_state = await self.oc.get_session(session_id)

                current_messages = session_state.get("messages", [])
                if len(current_messages) > last_message_count:
                    for msg in current_messages[last_message_count:]:
                        msg_content = msg.get("content", "")[:200] if msg.get("content") else ""
                        activity["messages"].append({
                            "role": msg.get("role"),
                            "content": msg_content,
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

                elapsed = time.time() - start_time
                if elapsed >= NEAR_TIMEOUT_THRESHOLD:
                    near_timeout_returned = True
                    activity["reasoning"].append(
                        f"Session still active after {int(elapsed)} seconds. "
                        "Use read_session_logs for full output."
                    )
                    break

                await asyncio.sleep(check_interval)
            except Exception as e:
                logger.error("wait_for_session_error", session_id=session_id, error=str(e))
                activity["error"] = str(e)
                break

        elapsed = time.time() - start_time
        activity["elapsed_seconds"] = int(elapsed)

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

        if near_timeout_returned:
            activity["still_active"] = True
            activity["flavor_text"] = (
                "*Session still active.* Use `read_session_logs` for detailed output "
                "or `wait_for_session` again to continue monitoring."
            )

        return activity
