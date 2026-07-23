# -*- coding: utf-8 -*-
"""기획 브레인스토밍 채팅 세션의 in-memory 저장소. jobs.py와 같은 패턴(스레드 락, DB 없음)."""
import threading
import uuid

_LOCK = threading.Lock()
_SESSIONS: dict[str, list[dict]] = {}
# ★2026-07-23(온보딩 B): 세션 → 그 세션이 만든 스튜디오 project_id. 기획 확정(finalize) 때
# 작품을 생성하고 여기 기록해, 재생성(finalize 재호출)이 새 작품을 또 만들지 않고 기존 작품을
# 갱신하게 한다(중복 작품 방지).
_SESSION_PROJECT: dict[str, str] = {}


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


def set_project(session_id: str, project_id: str) -> None:
    """이 세션이 만든 스튜디오 작품 id를 기록(온보딩 B — finalize 첫 호출 시)."""
    with _LOCK:
        _SESSION_PROJECT[session_id] = project_id


def get_project(session_id: str) -> str | None:
    """이 세션에 이미 만들어둔 작품 id(있으면). 재생성 시 그 작품을 갱신하려고 조회."""
    with _LOCK:
        return _SESSION_PROJECT.get(session_id)
