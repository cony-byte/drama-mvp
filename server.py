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
    generate_character_portrait, generate_conti, generate_key_scene_image, generate_pitch_card,
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
    role: str


@app.post("/api/portrait")
def portrait(req: PortraitRequest):
    """인물 1명의 초상 이미지를 만들어 base64 data URL로 반환. 세션과 무관한 stateless
    엔드포인트 — 카드 화면에서 인물별로 각각 비동기 호출한다."""
    png = generate_character_portrait({"name": req.name, "role": req.role})
    return {"image": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


class SceneImageRequest(BaseModel):
    situation: str


@app.post("/api/scene-image")
def scene_image(req: SceneImageRequest):
    """1화 임팩트 장면 미리보기 이미지. portrait와 동일하게 stateless·비동기 호출용."""
    png = generate_key_scene_image(req.situation)
    return {"image": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


class CharacterModel(BaseModel):
    name: str
    role: str = ""
    line: str = ""
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


class CharacterCreateRequest(BaseModel):
    name: str
    role: str = ""
    line: str = ""
    image: str | None = None


@app.post("/api/studio/{project_id}/characters")
def studio_add_character(project_id: str, req: CharacterCreateRequest):
    ch = studio.add_character(project_id, req.model_dump())
    if ch is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    return ch


class CharacterUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    line: str | None = None
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
        script = generate_script(idea, project["logline"], episode=num)
        studio.update_episode(project_id, num, script=script, stage="대본 완료")
    elif stage == "대본 완료":
        plan_text = generate_scene_plan(episode["script"], episode=num)
        scenes_plan = parsing.parse_plan_scenes(plan_text)
        if not scenes_plan:
            raise HTTPException(500, "씬 설계안에서 씬 목록을 파싱하지 못했어요.")
        studio.update_episode(project_id, num, plan_text=plan_text, scenes_plan=scenes_plan,
                             stage="씬설계 완료")
    elif stage == "씬설계 완료":
        # "콘티"(상세 스토리보드)는 GPT 이미지 생성용 내부 산출물이라 사용자에게 별도 단계로
        # 안 보여준다 — 씬설계 완료에서 곧장 샷분해 완료까지 이 한 번의 호출 안에서 처리한다.
        conti_full = generate_conti(episode["script"], episode["plan_text"],
                                    episode["scenes_plan"], episode=num)
        scenes = parsing.split_scenes(conti_full)
        if not scenes:
            raise HTTPException(500, "콘티에서 씬 헤더를 찾지 못했어요.")
        try:
            extract_and_register_elements(project["work"], conti_full)
        except Exception:
            pass  # 장소·소품·의상 자동 등록 실패해도 진행 자체는 막지 않음(그만큼 일관성 리스크)
        shots_by_scene = generate_shots_by_scene(scenes, work=project["work"])
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
