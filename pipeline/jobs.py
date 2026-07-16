# -*- coding: utf-8 -*-
"""in-memory job 상태 저장소. Redis/DB 없음 — 데모는 한 번에 1개 실행이면 충분하고,
프로세스 재시작 시 상태 유실은 감수(MVP 범위)."""
import threading
import uuid

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


def create() -> str:
    job_id = uuid.uuid4().hex
    with _LOCK:
        _JOBS[job_id] = {"status": "running", "stage": "대기 중", "video_url": None, "error": None}
    return job_id


def update(job_id: str, **fields) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


def get(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None
