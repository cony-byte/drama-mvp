# -*- coding: utf-8 -*-
"""데모용 API 서버. 로컬 Mac에서 그대로 돌리고 ngrok/Cloudflare Tunnel로 공개 URL을 붙여
GitHub Pages/Vercel에 올린 프론트(별도 배포)가 그 URL을 호출하는 구조 — CORS는 전부 허용
(데모 전용, 프로덕션 아님)."""
import base64
import threading

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline import chat, jobs, parsing, studio
from pipeline.orchestrator import (
    chat_reply, compose_idea_from_chat, extract_and_register_elements,
    generate_character_card, generate_character_portrait, generate_conti,
    generate_episode_summary, generate_key_scene_image, generate_pitch_card,
    generate_scene_plan, generate_script, generate_shots_by_scene, run_full_pipeline,
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    idea: str


class ChatStartRequest(BaseModel):
    idea: str


class ChatReplyRequest(BaseModel):
    message: str


def _start_pipeline(idea: str) -> str:
    job_id = jobs.create()
    threading.Thread(target=run_full_pipeline, args=(idea, job_id), daemon=True).start()
    return job_id


@app.post("/api/generate")
def generate(req: GenerateRequest):
    idea = req.idea.strip()
    if not idea:
        raise HTTPException(400, "아이디어를 입력해주세요.")
    return {"job_id": _start_pipeline(idea)}


@app.post("/api/chat/start")
def chat_start(req: ChatStartRequest):
    idea = req.idea.strip()
    if not idea:
        raise HTTPException(400, "아이디어를 입력해주세요.")
    session_id = chat.create()
    chat.append(session_id, "user", idea)
    reply, options = chat_reply(chat.get_history(session_id))
    chat.append(session_id, "assistant", reply)
    return {"session_id": session_id, "reply": reply, "options": options}


@app.post("/api/chat/{session_id}/reply")
def chat_continue(session_id: str, req: ChatReplyRequest):
    history = chat.get_history(session_id)
    if history is None:
        raise HTTPException(404, "채팅 세션을 찾을 수 없어요.")
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "메시지를 입력해주세요.")
    chat.append(session_id, "user", message)
    reply, options = chat_reply(chat.get_history(session_id))
    chat.append(session_id, "assistant", reply)
    return {"reply": reply, "options": options}


@app.post("/api/chat/{session_id}/finalize")
def chat_finalize(session_id: str):
    """텍스트 카드(로그라인+두 주인공)만 빠르게 만들어서 바로 보여준다 — 인물 이미지는
    포함 안 함(프론트가 화면 전환 직후 /api/portrait를 따로 불러서 채워 넣음, 그래야
    이미지 생성 때문에 화면 전환 자체가 느려지지 않는다). "재생성"도 이 엔드포인트를
    그대로 다시 호출한다."""
    history = chat.get_history(session_id)
    if history is None:
        raise HTTPException(404, "채팅 세션을 찾을 수 없어요.")
    idea = compose_idea_from_chat(history)
    return generate_pitch_card(idea)


class PortraitRequest(BaseModel):
    name: str
    role: str = ""
    gender: str = ""
    age: str = ""
    appearance: str = ""


@app.post("/api/portrait")
def portrait(req: PortraitRequest):
    """인물 1명의 초상 이미지를 만들어 base64 data URL로 반환. 세션과 무관한 stateless
    엔드포인트 — 카드 화면에서 인물별로 각각 비동기 호출한다. 이름·성별·나이·외형이
    서로 어긋나지 않게 한 프롬프트 안에서 같이 반영한다(generate_character_portrait 참고)."""
    png = generate_character_portrait(req.model_dump())
    return {"image": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


class SceneImageRequest(BaseModel):
    situation: str
    character_images: list[str] = []  # 인물 초상화 data URL — 장면에 같은 얼굴을 반영하기 위한 참조


@app.post("/api/scene-image")
def scene_image(req: SceneImageRequest):
    """1화 임팩트 장면 미리보기 이미지. portrait와 동일하게 stateless·비동기 호출용.
    character_images를 주면 그 인물 얼굴을 장면에 반영한다."""
    png = generate_key_scene_image(req.situation, character_images=req.character_images)
    return {"image": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


class CharacterModel(BaseModel):
    name: str
    gender: str = ""
    age: str = ""
    role: str = ""       # 포지션(직업/설정)
    line: str = ""       # 핵심대사
    appearance: str = "" # 외형
    description: str = "" # 설명
    image: str | None = None


class KeySceneModel(BaseModel):
    situation: str = ""
    lines: list[str] = []
    image: str | None = None


class StudioCreateRequest(BaseModel):
    idea: str
    logline: str
    characters: list[CharacterModel]
    key_scene: KeySceneModel | None = None


@app.post("/api/studio/create")
def studio_create(req: StudioCreateRequest):
    """온보딩 카드로 스튜디오 프로젝트를 만든다 — 1화가 빈 상태로 자동 생성되고,
    캐릭터 사진은 요소 레지스트리(얼굴 참조)로 등록된다(pipeline/studio.py 참고)."""
    card = req.model_dump()
    idea = card.pop("idea")
    project_id = studio.create_project(idea, card)
    return {"project_id": project_id}


@app.get("/api/studio/{project_id}")
def studio_get(project_id: str):
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    return project


class ProjectUpdateRequest(BaseModel):
    logline: str | None = None
    synopsis: str | None = None


@app.patch("/api/studio/{project_id}")
def studio_update(project_id: str, req: ProjectUpdateRequest):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    project = studio.update_project(project_id, **fields)
    if project is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    return project


class CharacterCreateRequest(BaseModel):
    name: str
    gender: str = ""
    age: str = ""
    role: str = ""
    line: str = ""
    appearance: str = ""
    description: str = ""
    image: str | None = None


@app.post("/api/studio/{project_id}/characters")
def studio_add_character(project_id: str, req: CharacterCreateRequest):
    ch = studio.add_character(project_id, req.model_dump())
    if ch is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    return ch


class CharacterGenerateRequest(BaseModel):
    name: str = ""
    hint: str = ""
    # 사용자가 이미 채운 값 — 채워진 칸은 유지하고 빈 칸만 AI가 생성
    gender: str = ""
    age: str = ""
    role: str = ""
    line: str = ""
    appearance: str = ""
    description: str = ""


@app.post("/api/studio/{project_id}/characters/generate")
def studio_generate_character(project_id: str, req: CharacterGenerateRequest):
    """이름(+선택 힌트)으로 캐릭터 카드 필드를 AI 생성. 이미 채운 칸은 그대로 두고 빈 칸만
    채운다. 저장은 안 하고 결과만 반환 — 프론트가 입력칸을 채우고 사용자가 검토 후 저장."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    existing = {k: getattr(req, k) for k in
                ("gender", "age", "role", "line", "appearance", "description")}
    try:
        fields = generate_character_card(
            req.name, hint=req.hint, logline=project.get("logline", ""),
            characters=project.get("characters", []), existing=existing,
        )
    except Exception as e:
        # 원시 500은 CORS 헤더가 안 붙어 브라우저에 "Failed to fetch"로만 보임 —
        # 명시적 HTTPException으로 감싸 CORS 미들웨어를 타고 제대로 된 에러 메시지가 가게 한다.
        raise HTTPException(502, f"AI 생성에 실패했어요. 잠시 후 다시 시도해주세요. ({e})")
    return fields


class CharacterUpdateRequest(BaseModel):
    name: str | None = None
    gender: str | None = None
    age: str | None = None
    role: str | None = None
    line: str | None = None
    appearance: str | None = None
    description: str | None = None
    image: str | None = None


@app.patch("/api/studio/{project_id}/characters/{char_id}")
def studio_update_character(project_id: str, char_id: str, req: CharacterUpdateRequest):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    ch = studio.update_character(project_id, char_id, **fields)
    if ch is None:
        raise HTTPException(404, "캐릭터를 찾을 수 없어요.")
    return ch


@app.delete("/api/studio/{project_id}/characters/{char_id}")
def studio_delete_character(project_id: str, char_id: str):
    if not studio.delete_character(project_id, char_id):
        raise HTTPException(404, "캐릭터를 찾을 수 없어요.")
    return {"ok": True}


@app.post("/api/studio/{project_id}/episodes")
def studio_add_episode(project_id: str):
    ep = studio.add_episode(project_id)
    if ep is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    return ep


class EpisodeUpdateRequest(BaseModel):
    subtitle: str | None = None
    summary: str | None = None
    script: str | None = None
    character_ids: list[str] | None = None


@app.patch("/api/studio/{project_id}/episodes/{num}")
def studio_update_episode(project_id: str, num: int, req: EpisodeUpdateRequest):
    if studio.get_episode(project_id, num) is None:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    studio.update_episode(project_id, num, **fields)
    return studio.get_episode(project_id, num)


@app.post("/api/studio/{project_id}/episodes/{num}/generate-script")
def studio_generate_script(project_id: str, num: int):
    """이 화 대본을 AI로 (재)생성. 이 화에 지정된 등장인물이 있으면 그 캐릭터들로,
    없으면 프로젝트 전체 캐릭터로 바이블을 구성해 이름·외형 일관성을 맞춘다."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    idea = project.get("idea") or project["logline"]
    ep_char_ids = set(episode.get("character_ids") or [])
    chars = [c for c in project["characters"] if c.get("id") in ep_char_ids] or project["characters"]
    script = generate_script(idea, project["logline"], episode=num, characters=chars)
    studio.update_episode(project_id, num, script=script)
    return studio.get_episode(project_id, num)


@app.post("/api/studio/{project_id}/episodes/{num}/generate-summary")
def studio_generate_summary(project_id: str, num: int):
    """이 화 대본을 바탕으로 요약을 AI로 (재)생성. 대본이 아직 없으면 400."""
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    if not episode.get("script"):
        raise HTTPException(400, "대본이 먼저 있어야 요약을 만들 수 있어요.")
    summary = generate_episode_summary(episode["script"])
    studio.update_episode(project_id, num, summary=summary)
    return studio.get_episode(project_id, num)


@app.post("/api/studio/{project_id}/episodes/{num}/advance")
def studio_advance_episode(project_id: str, num: int):
    """그 화의 현재 stage에서 딱 한 단계만 더 진행(대본→씬설계→콘티→샷분해). 이미지·영상·
    합본은 시간이 걸리는 단계라 별도 job 폴링 방식으로 다음 턴에 연결 예정."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")

    stage = episode["stage"]
    idea = project.get("idea") or project["logline"]

    if stage == "대본 대기":
        script = generate_script(idea, project["logline"], episode=num,
                                 characters=project["characters"])
        try:
            summary = generate_episode_summary(script)
        except Exception:
            summary = ""  # 실패해도 대본 완료 자체는 막지 않음 — 화면에서 빈 상태로 보임
        studio.update_episode(project_id, num, script=script, summary=summary, stage="대본 완료")
    elif stage == "대본 완료":
        plan_text = generate_scene_plan(episode["script"], episode=num,
                                        characters=project["characters"])
        scenes_plan = parsing.parse_plan_scenes(plan_text)
        if not scenes_plan:
            raise HTTPException(500, "씬 설계안에서 씬 목록을 파싱하지 못했어요.")
        studio.update_episode(project_id, num, plan_text=plan_text, scenes_plan=scenes_plan,
                             stage="씬설계 완료")
    elif stage == "씬설계 완료":
        # "콘티"(상세 스토리보드)는 GPT 이미지 생성용 내부 산출물이라 사용자에게 별도 단계로
        # 안 보여준다 — 씬설계 완료에서 곧장 샷분해 완료까지 이 한 번의 호출 안에서 처리한다.
        conti_full = generate_conti(episode["script"], episode["plan_text"],
                                    episode["scenes_plan"], episode=num,
                                    characters=project["characters"])
        scenes = parsing.split_scenes(conti_full)
        if not scenes:
            raise HTTPException(500, "콘티에서 씬 헤더를 찾지 못했어요.")
        try:
            extract_and_register_elements(project["work"], conti_full)
        except Exception:
            pass  # 장소·소품·의상 자동 등록 실패해도 진행 자체는 막지 않음(그만큼 일관성 리스크)
        shots_by_scene = generate_shots_by_scene(scenes, work=project["work"],
                                                 characters=project["characters"])
        studio.update_episode(project_id, num, conti_full=conti_full, scenes=scenes,
                             shots_by_scene=shots_by_scene, stage="샷분해 완료")
    else:
        raise HTTPException(501, f"'{stage}' 다음 단계는 아직 준비 중이에요.")

    return studio.get_episode(project_id, num)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job을 찾을 수 없어요.")
    resp = {"status": job["status"], "stage": job["stage"], "error": job.get("error")}
    if job.get("video_path"):
        resp["video_url"] = f"/api/jobs/{job_id}/video"
    return resp


@app.get("/api/jobs/{job_id}/video")
def get_video(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.get("video_path"):
        raise HTTPException(404, "아직 영상이 없어요.")
    return FileResponse(job["video_path"], media_type="video/mp4")


# 로컬에서 프론트까지 한 번에 확인하고 싶을 때용(배포 시엔 GitHub Pages/Vercel이 이 폴더를 대신 서빙).
app.mount("/", StaticFiles(directory="static", html=True), name="static")
