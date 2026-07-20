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

from pipeline import chat, jobs
from pipeline.orchestrator import (
    chat_reply, compose_idea_from_chat, generate_character_portrait, generate_pitch_card,
    run_full_pipeline,
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
