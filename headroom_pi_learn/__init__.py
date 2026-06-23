"""headroom-pi-plugin — headroom learn plugin for pi (earendil-works/pi-coding-agent).

Reads Pi session JSONL files from ~/.pi/agent/sessions/ and extracts
tool call failures for headroom learn analysis.

Pi session format:
  {"type": "session", "id": "...", "cwd": "..."}
  {"type": "message", "message": {"role": "assistant", "content": [
    {"type": "toolCall", "id": "...", "name": "read", "arguments": {...}}
  ]}}
  {"type": "message", "message": {"role": "toolResult", "toolCallId": "...",
    "toolName": "read", "content": [{"type": "text", "text": "..."}]}}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from headroom.learn._shared import classify_error, is_error_content, normalize_tool_name
from headroom.learn.base import ConversationScanner, LearnPlugin
from headroom.learn.models import (
    ErrorCategory,
    ProjectInfo,
    SessionData,
    SessionEvent,
    ToolCall,
)
from headroom.learn.plugins.claude import (
    ClaudeCodePlugin,
    _decode_project_path,
    _project_display_name,
)
from headroom.learn.writer import ClaudeCodeWriter, ContextWriter

logger = logging.getLogger(__name__)


class PiLearnPlugin(LearnPlugin, ConversationScanner):
    """headroom learn plugin for pi (earendil-works/pi-coding-agent).

    Reads JSONL session files from ~/.pi/agent/sessions/<encoded-project>/*.jsonl
    and extracts tool call failures for CLAUDE.md recommendations.
    """

    def __init__(self, pi_dir: Path | None = None):
        self.pi_dir = pi_dir or Path.home() / ".pi" / "agent"
        self.sessions_dir = self.pi_dir / "sessions"

    # ── LearnPlugin identity ───────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "pi"

    @property
    def display_name(self) -> str:
        return "pi (earendil-works)"

    @property
    def description(self) -> str:
        return "pi coding agent (~/.pi/agent/sessions/)"

    def detect(self) -> bool:
        return self.sessions_dir.exists() and any(self.sessions_dir.iterdir())

    def create_writer(self) -> ContextWriter:
        # Pi projects use CLAUDE.md — reuse Claude Code writer
        return ClaudeCodeWriter()

    # ── Project discovery ──────────────────────────────────────────────────

    def discover_projects(self) -> list[ProjectInfo]:
        """Discover all projects under ~/.pi/agent/sessions/.

        Pi uses the same --encoded-path-- directory naming as Claude Code.
        """
        if not self.sessions_dir.exists():
            return []

        projects: list[ProjectInfo] = []
        for entry in sorted(self.sessions_dir.iterdir()):
            if not entry.is_dir() or not any(entry.glob("*.jsonl")):
                continue

            # Pi uses --encoded-path-- (leading AND trailing '--').
            # Claude Code uses -encoded-path (leading '-' only).
            # Normalise to Claude Code format before decoding.
            encoded = entry.name
            if encoded.startswith("--") and encoded.endswith("--"):
                encoded = encoded.rstrip("-")[1:]  # '--foo-bar--' → '-foo-bar'

            project_path = _decode_project_path(encoded)
            if project_path is None:
                # Fallback: replace dashes with slashes
                project_path = Path("/" + entry.name.strip("-").replace("-", "/"))

            name = _project_display_name(project_path, entry.name)

            context_file: Path | None = None
            if project_path.exists():
                claude_md = project_path / "CLAUDE.md"
                if claude_md.exists():
                    context_file = claude_md

            projects.append(
                ProjectInfo(
                    name=name,
                    project_path=project_path,
                    data_path=entry,
                    context_file=context_file,
                    memory_file=None,
                )
            )

        return projects

    # ── Session scanning ───────────────────────────────────────────────────

    def scan_project(
        self,
        project: ProjectInfo,
        max_workers: int = 1,
        include_subagents: bool = True,
    ) -> list[SessionData]:
        jsonl_files = sorted(project.data_path.glob("*.jsonl"))
        if not jsonl_files:
            return []

        if max_workers <= 1 or len(jsonl_files) <= 1:
            return [
                s
                for f in jsonl_files
                if (s := self._scan_session(f)) and s.tool_calls
            ]

        from concurrent.futures import ThreadPoolExecutor, as_completed

        sessions: list[SessionData] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._scan_session, f): f for f in jsonl_files}
            for future in as_completed(futures):
                result = future.result()
                if result and result.tool_calls:
                    sessions.append(result)
        return sessions

    def _scan_session(self, jsonl_path: Path) -> SessionData | None:
        """Parse a single Pi JSONL session file.

        Pi format:
          assistant messages contain `toolCall` blocks
          toolResult messages contain the result for a given toolCallId
        """
        session_id = jsonl_path.stem
        # pending_calls: id → (name, arguments, msg_index)
        pending_calls: dict[str, tuple[str, dict, int]] = {}
        tool_calls: list[ToolCall] = []
        events: list[SessionEvent] = []
        msg_index = 0

        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    entry_type = entry.get("type", "")
                    ts = entry.get("timestamp")

                    if entry_type != "message":
                        continue

                    msg = entry.get("message", {})
                    role = msg.get("role", "")
                    content = msg.get("content", [])
                    msg_index += 1

                    # ── assistant: collect toolCall blocks ─────────────────
                    if role == "assistant" and isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "toolCall":
                                call_id = block.get("id", "")
                                name = block.get("name", "")
                                args = block.get("arguments", {})
                                if call_id and name:
                                    pending_calls[call_id] = (name, args if isinstance(args, dict) else {}, msg_index)

                    # ── user: collect regular user messages ────────────────
                    elif role == "user" and isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    if "[Request interrupted by user" in text:
                                        events.append(SessionEvent(
                                            type="interruption",
                                            msg_index=msg_index,
                                            timestamp=ts,
                                            text=text[:200],
                                        ))
                                    else:
                                        events.append(SessionEvent(
                                            type="user_message",
                                            msg_index=msg_index,
                                            timestamp=ts,
                                            text=text[:500],
                                        ))

                    # ── toolResult: match back to pending call ─────────────
                    elif role == "toolResult":
                        call_id = msg.get("toolCallId", "")
                        tool_name = msg.get("toolName", "")

                        # Extract text content
                        result_text = ""
                        if isinstance(content, list):
                            parts = []
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    parts.append(block.get("text", ""))
                            result_text = "\n".join(parts)
                        elif isinstance(content, str):
                            result_text = content

                        # Resolve call metadata from pending_calls
                        if call_id in pending_calls:
                            name, args, call_msg_idx = pending_calls.pop(call_id)
                        else:
                            name = tool_name or "unknown"
                            args = {}
                            call_msg_idx = msg_index

                        explicit_error = msg.get("isError", False)
                        detected_error = is_error_content(result_text)
                        is_err = explicit_error or detected_error
                        error_cat = classify_error(result_text) if is_err else ErrorCategory.UNKNOWN

                        tc = ToolCall(
                            name=normalize_tool_name(name),
                            tool_call_id=call_id or f"pi_{msg_index}",
                            input_data=args,
                            output=result_text,
                            is_error=is_err,
                            error_category=error_cat,
                            msg_index=call_msg_idx,
                            output_bytes=len(result_text.encode("utf-8")),
                        )
                        tool_calls.append(tc)
                        events.append(SessionEvent(
                            type="tool_call",
                            msg_index=call_msg_idx,
                            timestamp=ts,
                            tool_call=tc,
                        ))

        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Failed to read %s: %s", jsonl_path, exc)
            return None

        return SessionData(
            session_id=session_id,
            tool_calls=tool_calls,
            events=events,
        )


# Module-level instance — auto-discovered by headroom's plugin registry
plugin = PiLearnPlugin()
