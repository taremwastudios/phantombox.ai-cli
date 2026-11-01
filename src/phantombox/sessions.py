"""Chat session persistence (save, list, restore, delete) for PhantomBox."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from phantombox.env import get_data_dir

SESSIONS_DIR = get_data_dir() / "sessions"


def _ensure_sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def list_saved_sessions() -> List[Dict[str, Any]]:
    """List all saved chat sessions ordered by timestamp (newest first)."""
    sdir = _ensure_sessions_dir()
    sessions = []
    for file in sdir.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sessions.append({
                    "id": file.stem,
                    "filename": file.name,
                    "name": data.get("name", file.stem),
                    "model": data.get("model", "unknown"),
                    "message_count": len(data.get("messages", [])),
                    "timestamp": data.get("timestamp", file.stat().st_mtime),
                })
        except Exception:
            continue

    sessions.sort(key=lambda s: s["timestamp"], reverse=True)
    return sessions


def save_chat_session(name: str, model: str, messages: List[Dict[str, Any]]) -> str:
    """Save conversation messages to a session file. Returns session ID."""
    sdir = _ensure_sessions_dir()
    safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name.strip())
    if not safe_name:
        safe_name = f"chat_{int(time.time())}"

    session_id = f"{safe_name}_{int(time.time())}"
    filepath = sdir / f"{session_id}.json"

    data = {
        "id": session_id,
        "name": name.strip() or safe_name,
        "model": model,
        "timestamp": time.time(),
        "messages": messages,
    }
    filepath.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return session_id


def load_chat_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Load a chat session by ID or filename stem."""
    sdir = _ensure_sessions_dir()
    target = sdir / f"{session_id}.json"
    if not target.exists():
        # Search by stem
        for f in sdir.glob("*.json"):
            if f.stem == session_id:
                target = f
                break
    if not target.exists():
        return None

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data
    except Exception:
        return None


def delete_chat_session(session_id: str) -> bool:
    """Delete a saved chat session."""
    sdir = _ensure_sessions_dir()
    target = sdir / f"{session_id}.json"
    if not target.exists():
        for f in sdir.glob("*.json"):
            if f.stem == session_id:
                target = f
                break
    if target.exists():
        try:
            target.unlink()
            return True
        except Exception:
            return False
    return False
