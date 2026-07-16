# -*- coding: utf-8 -*-
"""한 줄 아이디어 → 기획안 → 대본 → 씬설계 → 상세콘티 → 샷분해 (1~5단계, 텍스트만).
이미지·영상·합본(6~8단계)은 나중에 이어붙인다. 모든 LLM/HTTP 호출은 vendor의 기존 함수 재사용."""
import vendor.cowriter.bot.generator as cw_generator
import vendor.cowriter.bot.prompts as cw_prompts
import vendor.storyboard.bot.generator as sb_generator
import vendor.storyboard.bot.openrouter_image as oi
import vendor.storyboard.bot.openrouter_video as hf_video
import vendor.storyboard.bot.prompts as sb_prompts
import vendor.storyboard.bot.vp_store as vp_store

from pipeline import parsing


def _with_retry(fn, *args, retries: int = 1, **kwargs):
    """agent 백엔드(로컬 claude CLI 호출)가 가끔 'Claude Code returned an error result: success'
    같은 일시적 오류로 실패하는 걸 실측 — 재시도하면 대개 바로 성공한다. 그 외 원인 불명 오류를
    한 번만 더 시도해 데모 중 파이프라인이 죽는 걸 줄인다(그 이상 재시도 인프라는 안 만듦)."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                continue
    raise last_exc


def generate_pitch(idea: str) -> str:
    user_msg = f"이 컨셉으로 기획안 초안을 만들어줘:\n{idea}"
    return _with_retry(cw_generator.complete, cw_prompts.plan_system(idea), user_msg).strip()


def generate_script(idea: str, pitch: str, episode: int = 1) -> str:
    thread_messages = [{"role": "user",
                         "content": f"{pitch}\n\n위 기획안을 바탕으로 {episode}화 대본을 써줘."}]
    return _with_retry(cw_generator.generate, thread_messages, idea, bible=None,
                       target_episode=episode, kind="대본").strip()


def generate_scene_plan(script: str, episode: int = 1) -> str:
    return _with_retry(
        sb_generator.complete,
        sb_prompts.storyboard_plan_system(bible=None, target_episode=episode),
        sb_prompts.storyboard_plan_user(script)).strip()


def generate_conti(script: str, plan_text: str, scenes_plan: list[tuple[int, str]],
                   episode: int = 1) -> str:
    """상세 콘티. app.py의 씬 단위 병렬 호출(_gen_scene)과 같은 프롬프트 패턴이지만
    MVP는 순차 for-loop로(디버깅 쉬움, 데모 안정성 우선)."""
    sys_prompt = sb_prompts.storyboard_system(bible=None, target_episode=episode)
    parts = []
    for num, line in scenes_plan:
        user = (
            f"[씬 설계안 — 화 전체 목록(참고용, 다른 씬은 이미 별도로 처리 중이니 이 씬에만 집중)]\n"
            f"{plan_text}\n\n"
            f"[원본 대본 — 사건·행동·대사 하나도 바꾸지 마라]\n{script}\n\n"
            f"(지금은 화 전체가 아니라 이 씬 하나만 상세 콘티로 써라: '{line}'. "
            f"반드시 '■ 씬{num} · N초 · 제목' 헤더로 시작해 이 씬의 샷 콘티만 출력하고 다른 씬은 "
            "언급하지 마라. 대본의 사건·행동·대사는 하나도 바꾸지 마라.)"
        )
        parts.append(_with_retry(sb_generator.complete, sys_prompt, user).strip())
    return "\n\n".join(parts)


def generate_shots_by_scene(scenes: list[tuple[int, str, str]]) -> dict[int, list[dict]]:
    """씬별 상세콘티 body를 샷 단위로 분해. 반환: {씬번호: [shot dict, ...]}.
    OPENROUTER_API_KEY 필요(agent 백엔드 아니라 OpenRouter chat 사용, 원본과 동일)."""
    shots_by_scene = {}
    for num, _hdr, body in scenes:
        raw = _with_retry(oi.chat, sb_prompts.storyboard_shots_system(bible=None),
                          sb_prompts.storyboard_shots_user(body))
        shots = [s for s in parsing.parse_json_array(raw) if s.get("prompt")]
        for i, s in enumerate(shots, 1):
            s["n"] = i
        shots_by_scene[num] = shots
    return shots_by_scene


def generate_image_for_shot(shot: dict) -> tuple[bytes, float]:
    """샷 하나의 스틸컷 생성 → (PNG bytes, cost$). 요소 레지스트리 참조(refs)는 MVP에서
    스킵(하드 게이트 아님 — 얼굴 일관성은 이번 데모에서 포기)."""
    return _with_retry(oi.generate, shot["prompt"], aspect_ratio="9:16", refs=[])


def generate_video_for_cut(work: str, scene_num: int, cut_num: int, png: bytes,
                           motion_prompt: str, episode: int = 1) -> str:
    """스틸컷 1장을 영상화해 로컬 mp4 절대경로를 반환. project_setup.ensure_project(work)를
    먼저 호출해둬야 vp_store가 프로젝트 디렉토리를 찾을 수 있다."""
    url, cost = _with_retry(hf_video.generate, png, motion_prompt,
                            aspect_ratio="9:16", generate_audio=False)
    path = vp_store.save_video(work, scene_num=scene_num, cut_num=cut_num, url=url,
                               episode=episode, cost=cost)
    if not path:
        raise RuntimeError(f"영상 다운로드/저장 실패 (씬{scene_num} 컷{cut_num})")
    return path


def run_text_stages(idea: str, episode: int = 1, on_stage=None) -> dict:
    """1~5단계(텍스트만) 실행. on_stage(stage_name)는 단계 전환 시 호출되는 진행 알림 콜백."""
    def notify(stage):
        if on_stage:
            on_stage(stage)

    notify("기획안 작성 중")
    pitch = generate_pitch(idea)

    notify("대본 작성 중")
    script = generate_script(idea, pitch, episode=episode)

    notify("씬 설계 중")
    plan_text = generate_scene_plan(script, episode=episode)
    scenes_plan = parsing.parse_plan_scenes(plan_text)
    if not scenes_plan:
        raise RuntimeError("씬 설계안에서 씬 목록을 파싱하지 못했어요 — 출력 형식이 예상과 달라요.")

    notify("상세 콘티 작성 중")
    conti_full = generate_conti(script, plan_text, scenes_plan, episode=episode)
    scenes = parsing.split_scenes(conti_full)
    if not scenes:
        raise RuntimeError("콘티에서 씬 헤더('■ 씬N')를 찾지 못했어요.")

    notify("샷 분해 중")
    shots_by_scene = generate_shots_by_scene(scenes)

    return {
        "idea": idea,
        "episode": episode,
        "pitch": pitch,
        "script": script,
        "plan_text": plan_text,
        "conti_full": conti_full,
        "scenes": scenes,
        "shots_by_scene": shots_by_scene,
    }
