# -*- coding: utf-8 -*-
"""기획 브레인스토밍 채팅 세션의 in-memory 저장소. jobs.py와 같은 패턴(스레드 락, DB 없음)."""
import threading
import uuid

_LOCK = threading.Lock()
_SESSIONS: dict[str, list[dict]] = {}


def create() -> str:
    session_id = uuid.uuid4().hex
    with _LOCK:
        _SESSIONS[session_id] = []
    return session_id


def append(session_id: str, role: str, content: str) -> None:
    with _LOCK:
        if session_id in _SESSIONS:
            _SESSIONS[session_id].append({"role": role, "content": content})


def get_history(session_id: str) -> list[dict] | None:
    with _LOCK:
        history = _SESSIONS.get(session_id)
        return list(history) if history is not None else None
