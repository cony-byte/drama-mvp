# -*- coding: utf-8 -*-
"""한 줄 아이디어 → 기획안 → 대본 → 씬설계 → 상세콘티 → 샷분해 (1~5단계, 텍스트만).
이미지·영상·합본(6~8단계)은 나중에 이어붙인다. 모든 LLM/HTTP 호출은 vendor의 기존 함수 재사용."""
import vendor.cowriter.bot.generator as cw_generator
import vendor.cowriter.bot.prompts as cw_prompts
import vendor.storyboard.bot.edit_plan as edit_plan
import vendor.storyboard.bot.episode_compile as episode_compile
import vendor.storyboard.bot.generator as sb_generator
import vendor.storyboard.bot.openrouter_image as oi
import vendor.storyboard.bot.openrouter_video as hf_video
import vendor.storyboard.bot.prompts as sb_prompts
import vendor.storyboard.bot.video_index as video_index
import vendor.storyboard.bot.vp_store as vp_store

from pipeline import jobs, parsing, project_setup


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


CHAT_SYSTEM = """너는 숏폼 드라마 기획을 같이 구상하는 친근하고 리액션 좋은 파트너다.
지금은 로맨스만 프로덕션 가능(레퍼런스DB·템플릿이 로맨스 전용) — 나중에 다른 장르가 추가될
수도 있지만, 지금은 항상 로맨스를 뼈대로 두고 그 안에서 좁혀라. "로맨스냐 스릴러냐"처럼 로맨스
자체를 벗어나는 선택지는 주지 말고, 스릴러/서스펜스 같은 느낌은 로맨스 안의 텐션 정도로만 다뤄라.

사용자의 아이디어나 답변에 짧고 긍정적으로 반응해라("오 좋은데요!" 같은 톤) — 그 소재가 어떤
장르·트렌드에 해당하는지 아는 티를 내면 더 좋다(예: "재벌x알바생 조합, 로맨스 클리셰 중에서도
스테디셀러죠").

매 턴마다 아래 순서로 아직 안 정해진 축을 딱 1개만 좁혀라(이미 아이디어/답변에서 드러난 축은
다시 묻지 말고 그거라고 짚어주기만 하고 다음 축으로 넘어가라):
1. 서브장르/설정(오피스·사극·학원물·재벌물·아이돌물 등) — 이미 명확하면 스킵. 애매하면 "이거
   좋아요?" 식 열린 질문 대신, 구체적인 선택지 2~3개를 추천해라(예: "오피스 로맨스로 갈지,
   아예 사극으로 바꿔볼지 — 어느 쪽이 좋아요?").
2. 핵심 갈등/훅 — 뭐가 두 사람을 못 만나게 막는지.
3. 텐션/톤 — 코미디/절절함/다크, 그리고 서스펜스·스릴러 느낌을 얼마나 섞을지(예: "이 갈등, 잔잔한
   신파로 갈지 스릴러처럼 긴장감 있게 갈지?"). 어느 쪽이든 결말은 로맨스로 수렴한다.
4. 엔딩 방향 — 해피/새드/열린.

2~3문장 이내로 짧게, 채팅처럼 답하라 — 기획안·대본 형식(제목·항목·목록)으로 쓰지 마라.
3~4번 정도 대화가 오갔으면(위 4가지가 대부분 정해졌으면), 이제 구체적인 기획안을 써볼 만하다고
자연스럽게 제안해라."""


def chat_reply(history: list[dict]) -> str:
    """history: [{"role": "user"|"assistant", "content": str}, ...], 마지막이 사용자 메시지."""
    lines = [f"[{'사용자' if m['role'] == 'user' else '너'}] {m['content']}" for m in history]
    convo = "\n".join(lines)
    return _with_retry(cw_generator.complete, CHAT_SYSTEM, convo).strip()


def compose_idea_from_chat(history: list[dict]) -> str:
    """채팅으로 주고받은 내용을 한 덩어리 컨셉 텍스트로 합쳐 기존 파이프라인의 idea로 사용."""
    lines = [f"{'사용자' if m['role'] == 'user' else '보조'}: {m['content']}" for m in history]
    return "다음은 기획 방향을 잡기 위해 나눈 대화다. 이 내용을 종합해 기획안을 써라.\n\n" + "\n".join(lines)


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


def generate_cuts_for_scene(work: str, scene_num: int, shots: list[dict],
                            episode: int = 1, on_progress=None) -> list[dict]:
    """씬의 모든 샷을 순서대로 이미지→영상 생성. 안전필터 등으로 실패한 컷은 그 컷만
    건너뛰고 나머지를 계속 진행(전체 실패 방지만 목적 — 재시도 고도화는 안 함, MVP 범위).
    반환: [{"cut_num", "status": "ok"|"failed", "video_path"?, "error"?}, ...]"""
    results = []
    for shot in shots:
        cut_num = shot["n"]

        def notify(msg):
            if on_progress:
                on_progress(scene_num, cut_num, msg)

        try:
            notify("이미지 생성 중")
            png, _img_cost = generate_image_for_shot(shot)
            notify("영상화 중")
            path = generate_video_for_cut(work, scene_num, cut_num, png,
                                          motion_prompt=shot.get("caption", shot["prompt"]),
                                          episode=episode)
            results.append({"cut_num": cut_num, "status": "ok", "video_path": path})
            notify("완료")
        except Exception as e:
            results.append({"cut_num": cut_num, "status": "failed", "error": str(e)})
            notify(f"실패 — 건너뜀: {e}")
    return results


def generate_mood_prompt(idea: str) -> str:
    """배경음악(Lyria) 프롬프트용 짧은 영어 무드 묘사 한 문장."""
    system = ("You write ONE short English sentence describing background-music mood/style "
              "for a short-form romance drama scene, for a text-to-music generator. "
              "Output only the sentence — no quotes, no extra text.")
    return _with_retry(sb_generator.complete, system, f"Story concept: {idea}").strip()


def attach_narration(plan: list[dict], shots_by_scene: dict[int, list[dict]]) -> list[dict]:
    """edit_plan.build_edit_plan()의 결과는 narration_text가 항상 None(LLM 편집계획을 안 쓰는
    러프 플랜이라 애초에 안 채워짐) — 각 컷의 샷 caption을 나레이션 텍스트로 채워 넣는다."""
    for seg in plan:
        shots = shots_by_scene.get(seg["scene_num"]) or []
        shot = next((s for s in shots if s.get("n") == seg["cut_num"]), None)
        if shot and shot.get("caption"):
            seg["narration_text"] = shot["caption"]
            seg["speaker"] = "나레이션"
    return plan


def compile_episode_video(work: str, idea: str, scenes: list[tuple[int, str, str]],
                          shots_by_scene: dict[int, list[dict]], episode: int = 1,
                          episode_title: str = "1화") -> str:
    """지금까지 저장된 영상들을 모아 나레이션·배경음악까지 믹싱한 draft mp4를 만든다."""
    scene_nums = [num for num, _hdr, _body in scenes]
    videos_by_scene = video_index.list_episode_videos(work, scene_nums, episode=episode)
    plan = edit_plan.build_edit_plan(work, episode_title, scenes, videos_by_scene)
    if not plan:
        raise RuntimeError("영상화된 컷이 하나도 없어서 합본할 수 없어요.")
    plan = attach_narration(plan, shots_by_scene)
    mood_prompt = generate_mood_prompt(idea)
    return episode_compile.compile_episode(work, episode_title, plan, mood_prompt=mood_prompt)


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


def run_full_pipeline(idea: str, job_id: str, episode: int = 1) -> None:
    """1~8단계 전체(기획안~합본)를 실행하며 jobs 스토어에 진행상황을 기록. 백그라운드
    스레드에서 호출되는 걸 전제로 예외를 삼키고 jobs에 error로 남긴다(서버가 프로세스
    자체가 죽지 않게)."""
    work = f"demo-{job_id[:8]}"
    try:
        result = run_text_stages(idea, episode=episode,
                                 on_stage=lambda stage: jobs.update(job_id, stage=stage))

        project_setup.ensure_project(work)

        scenes = result["scenes"]
        shots_by_scene = result["shots_by_scene"]
        total_shots = sum(len(shots) for shots in shots_by_scene.values())
        done_count = 0

        def on_progress(scene_num, cut_num, msg):
            nonlocal done_count
            if msg in ("완료",) or msg.startswith("실패"):
                done_count += 1
            jobs.update(job_id, stage=f"영상 제작 중 ({done_count}/{total_shots}컷) — 씬{scene_num} 컷{cut_num}: {msg}")

        for scene_num, _hdr, _body in scenes:
            shots = shots_by_scene.get(scene_num) or []
            if shots:
                generate_cuts_for_scene(work, scene_num, shots, episode=episode, on_progress=on_progress)

        jobs.update(job_id, stage="합본 중")
        draft_path = compile_episode_video(work, idea, scenes, shots_by_scene, episode=episode)

        jobs.update(job_id, status="done", stage="완료", video_path=draft_path)
    except Exception as e:
        jobs.update(job_id, status="error", stage="오류", error=str(e))
