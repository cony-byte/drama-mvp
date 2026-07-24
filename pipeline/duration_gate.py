"""[확정] → 스켈레톤 분량 게이트 (2026-07-23, project_drama_duration_gate 구현).

대본 '확정' 시점에 스켈레톤(3단계 뼈대)을 생성해 씬별 declared_seconds 합으로 화 전체
러닝타임을 측정하고 90~130초(v3_schema.EPISODE_SECONDS_MIN/MAX, 목표 120초) 게이트를 판정한다.
범위를 벗어나면 AI 자동맞춤을 제안한다: 길면 압축(compress) → 압축해도 안 되면 분할(split),
짧으면 확장(expand). script_source 플래그 없이 '확정' 시점에 모든 대본을 일괄 측정한다.

측정은 스켈레톤 LLM 1회로 끝난다(상세콘티·이미지·영상은 만들지 않음) — 수 초~십수 초.
"""
from __future__ import annotations

from . import v3_schema
from . import orchestrator as orch

# 목표 러닝타임 — 화당 분량 목표를 상한(120초)에 맞춘다. 압축이 100초로 깎아 너무 짧아지던 문제로
# 2026-07-24 100→120으로 올림. 압축 프롬프트는 "최대 120초"를 함께 지시해 상한을 넘지 않게 한다.
TARGET_SECONDS = 120


def measure(script: str, episode: int = 1, characters: list[dict] | None = None,
            skeleton_text: str | None = None) -> dict:
    """스켈레톤으로 화 전체 러닝타임을 측정. 반환:
    {total, min, max, target, verdict('ok'|'over'|'under'), scenes:[{num,seconds,title}], skeleton}
    ★skeleton_text가 주어지면(대본이 안 바뀌어 캐시된 뼈대) 그걸 파싱만 해 LLM 없이 즉시 측정한다.
    없으면 LLM 1회로 새로 만든다 — 매 클릭 재생성/느린 터널 타임아웃 방지.
    ★측정 스켈레톤은 honest_timing=True(캡 없이 실제 필요 초)로 만든다. 캡을 건 제작용 스켈레톤을
    쓰면 긴 대본도 범위 안으로 눌러담겨 항상 'ok'가 나오기 때문(측정 무의미). 이 정직 스켈레톤은
    측정 전용이며, 제작(≤130초)에는 별도로 캡 걸린 스켈레톤을 쓴다."""
    if skeleton_text:
        text = skeleton_text
        scenes = [v3_schema.parse_scene(hdr, body)
                  for _, hdr, body in orch.parsing.split_scenes(text)]
    else:
        text, scenes, _errors = orch.generate_episode_skeleton_validated(
            script, episode=episode, characters=characters, honest_timing=True)
    per = [{"num": s.get("scene_num"),
            "seconds": round(s.get("declared_seconds") or 0, 1),
            "title": s.get("title")} for s in scenes]
    total = round(sum(p["seconds"] for p in per), 1)
    lo, hi = v3_schema.EPISODE_SECONDS_MIN, v3_schema.EPISODE_SECONDS_MAX
    verdict = "under" if total < lo else "over" if total > hi else "ok"
    return {"total": total, "min": lo, "max": hi, "target": TARGET_SECONDS,
            "verdict": verdict, "scenes": per, "skeleton": text}


# ── AI 자동맞춤(대본 재작성 → 재측정) ─────────────────────────────────────────
# 대본 텍스트 자체엔 초가 없다(스켈레톤이 초를 배분). 그래서 자동맞춤은 '대본'을 재작성하고
# 다시 measure()로 재측정한다. LLM이 한 번에 목표에 정확히 안 맞을 수 있어 결과 분량을 함께
# 돌려주고, 상위(프론트)가 '압축해도 길면 분할할까요?'를 사용자에게 물어 선택하게 한다.

_SYS_COMMON = (
    "너는 한국 세로형 숏폼 웹드라마 대본 분량 조정 전문가야. 대본의 씬 구조·헤더 표기·인물"
    "이름·핵심 대사와 감정선·이야기 흐름은 반드시 보존하고, 대본 '포맷'(씬 헤더, 이름: 대사,"
    "(지문) 등)은 그대로 유지해. 초·러닝타임 숫자는 대본에 쓰지 마(분량은 뒤 단계가 계산). "
    "설명·머리말 없이 조정된 대본 전문만 출력해.")

_SYS = {
    "compress": (_SYS_COMMON + " 지금 대본이 목표보다 길어. 늘어지는 지문·부차 대사·반복·"
                 "군더더기 비트를 쳐내 분량을 약 {target}초 분량(최대 {hi}초)으로 압축해. "
                 "씬을 통째로 지우기보다 각 씬을 조여 자연스럽게 줄여."),
    "expand":   (_SYS_COMMON + " 지금 대본이 목표보다 짧아. 이야기에 맞는 대사·행동 비트·감정"
                 "표현을 자연스럽게 더해 분량을 약 {target}초 분량(최소 {lo}초)으로 늘려."),
    "split":    (_SYS_COMMON + " 이 대본은 한 화(약 {target}초)에 담기엔 길어. 이야기가 자연스럽게"
                 " 끊기는 지점에서 1화와 2화로 나눠라. **1화만** 출력하되 약 {target}초 분량에"
                 " 맞고 다음 화가 궁금해지는 훅으로 끝나게 해."),
}


def autofit(script: str, *, mode: str = "compress", episode: int = 1,
            characters: list[dict] | None = None, target: int = TARGET_SECONDS) -> dict:
    """대본을 mode(compress|expand|split)로 재작성하고 재측정. 반환:
    {mode, script(새 대본), measure(measure() 결과)}."""
    lo, hi = v3_schema.EPISODE_SECONDS_MIN, v3_schema.EPISODE_SECONDS_MAX
    system = _SYS[mode].format(target=target, lo=lo, hi=hi)
    user = f"[대본]\n{script}\n\n위 대본을 지시대로 조정해서 조정된 대본 전문만 출력해."
    new_script = orch._with_retry(orch._sb_complete, system, user).strip()
    return {"mode": mode, "script": new_script,
            "measure": measure(new_script, episode=episode, characters=characters)}
