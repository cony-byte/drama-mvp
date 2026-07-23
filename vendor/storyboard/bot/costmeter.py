"""에피소드 1편 제작의 총 비용($)·건수 계측기.

이미지/영상/LLM/TTS 생성이 모두 거쳐가는 벤더 래퍼(openrouter_image.generate/chat,
openrouter_video.generate, openrouter_tts.synthesize)가 각 호출 끝에서 이 계측기에 비용을
누적한다. 스레드 안전이라 클립 병렬 생성 스레드풀(orchestrator.produce_scene)에서도 정확하다.

에피소드 제작 경계(orchestrator.produce_episode_v3_job)에서 reset()으로 0으로 돌리고, 제작이
끝나면 snapshot()으로 그 구간 합계를 읽어 로그에 남긴다.

★전제: 한 번에 한 편만 제작(내부 데모). 동시에 여러 편을 제작하면 전역 계측이라 합계가 섞인다.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_data: dict[str, list] = {}  # kind -> [usd_sum, count]


def add(kind: str, usd: float = 0.0) -> None:
    """생성 1건을 누적한다. kind='image'|'video'|'llm'|'tts' 등, usd=그 호출 비용($, 모르면 0)."""
    with _lock:
        e = _data.setdefault(kind, [0.0, 0])
        try:
            e[0] += float(usd or 0.0)
        except (TypeError, ValueError):
            pass  # 비용 파싱 실패해도 건수는 센다
        e[1] += 1


def reset() -> None:
    """구간 시작 — 누적값을 모두 0으로."""
    with _lock:
        _data.clear()


def snapshot() -> dict:
    """현재 누적 스냅샷: {'total_usd': float, 'by_kind': {kind: {'usd':.., 'n':..}}}."""
    with _lock:
        by = {k: {"usd": round(v[0], 6), "n": v[1]} for k, v in _data.items()}
    return {"total_usd": round(sum(x["usd"] for x in by.values()), 6), "by_kind": by}


def format_summary(snap: dict | None = None) -> str:
    """스냅샷을 사람이 읽는 한 줄로. 예: '$2.4567 (이미지 27건 $1.2000 · 영상 24건 $1.2500 · …)'."""
    snap = snap or snapshot()
    _label = {"image": "이미지", "video": "영상", "llm": "LLM", "tts": "TTS"}
    parts = []
    for kind, v in snap["by_kind"].items():
        name = _label.get(kind, kind)
        parts.append(f"{name} {v['n']}건 ${v['usd']:.4f}")
    detail = (" (" + " · ".join(parts) + ")") if parts else ""
    return f"${snap['total_usd']:.4f}{detail}"
