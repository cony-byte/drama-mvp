# -*- coding: utf-8 -*-
"""데모용 API 서버. 로컬 Mac에서 그대로 돌리고 ngrok/Cloudflare Tunnel로 공개 URL을 붙여
GitHub Pages/Vercel에 올린 프론트(별도 배포)가 그 URL을 호출하는 구조 — CORS는 전부 허용
(데모 전용, 프로덕션 아님)."""
import base64
import os
import threading


# .env를 프로세스 환경으로 로드(python-dotenv 의존성 없이). vendor/.../config.py가 import 시점에
# os.environ에서 OPENROUTER_API_KEY 등을 읽으므로, 반드시 pipeline import보다 먼저 실행돼야 한다.
# 이미 환경에 설정된 값은 덮어쓰지 않는다(실제 환경변수/셸 export 우선).
def _load_dotenv(name: str = ".env") -> None:
    import pathlib
    p = pathlib.Path(__file__).resolve().parent / name
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline import chat, jobs, parsing, studio
from pipeline import duration_gate
from functools import partial

from pipeline.orchestrator import (
    chat_reply, compose_idea_from_chat, extract_and_register_elements,
    generate_character_card, generate_character_portrait, generate_conti,
    generate_episode_plan_summary, generate_episode_summary, generate_key_scene_image,
    generate_pitch_card, generate_scene_plan, generate_script, generate_shots_by_scene,
    generate_stills_for_scene, generate_synopsis, looks_like_clarification,
    preview_scene_v3, produce_episode_v3_job, produce_episode_video, regenerate_cut_still,
    run_full_pipeline, studio_script_bible, videoize_cut_job,
)
from pipeline.orchestrator import _script_signature

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _owner(request: Request) -> str:
    """로그인이 없으니 클라이언트 IP를 소유자 키로 쓴다. Cloudflare/터널 뒤에서는 실제 IP가
    X-Forwarded-For 첫 항목(또는 CF-Connecting-IP)으로 들어오므로 그걸 우선한다."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    return request.client.host if request.client else "unknown"


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
def chat_finalize(session_id: str, request: Request):
    """텍스트 카드(로그라인+두 주인공)를 만들고 ★그 시점에 스튜디오 작품을 생성한다(온보딩 B).
    - 첫 호출: create_project로 새 작품 생성 + 세션↔작품 매핑 기록(chat.set_project).
    - 재생성(같은 세션 재호출): 기존 작품을 갱신(로그라인·인물·키장면) — 새 작품을 또 만들지
      않는다(중복 방지). 시놉시스는 첫 생성 때만 만든다(재생성을 빠르게 — 시놉시스는 카드에
      안 쓰이고 스튜디오에서 재생성 버튼으로 갱신 가능).
    인물 이미지는 여기 포함 안 함 — 프런트가 화면 전환 후 /api/portrait로 채우고, '스튜디오로
    이동' 때 편집분(이미지 포함)을 PATCH로 반영한다. 반환에 project_id를 실어 보낸다."""
    history = chat.get_history(session_id)
    if history is None:
        raise HTTPException(404, "채팅 세션을 찾을 수 없어요.")
    idea = compose_idea_from_chat(history)
    card = generate_pitch_card(idea)
    pid = chat.get_project(session_id)
    if pid and studio.get_project(pid):
        studio.update_project(pid, title=card.get("title", ""),
                              logline=card.get("logline", ""),
                              characters=card.get("characters") or [],
                              key_scene=card.get("key_scene"))
    else:
        pid = studio.create_project(idea, dict(card), owner=_owner(request))
        chat.set_project(session_id, pid)
    return {**card, "project_id": pid}


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
def studio_create(req: StudioCreateRequest, request: Request):
    """온보딩 카드로 스튜디오 프로젝트를 만든다 — 1화가 빈 상태로 자동 생성되고,
    캐릭터 사진은 요소 레지스트리(얼굴 참조)로 등록된다(pipeline/studio.py 참고)."""
    card = req.model_dump()
    idea = card.pop("idea")
    project_id = studio.create_project(idea, card, owner=_owner(request))
    return {"project_id": project_id}


@app.post("/api/studio/seed")
def studio_seed(request: Request):
    """테스트용 더미 프로젝트를 즉시 만들어 project_id를 반환(AI 호출 없음). 매번 캐릭터·대본을
    새로 채우는 수고 없이 바로 스튜디오를 열어보기 위한 개발 편의 기능."""
    return {"project_id": studio.seed_demo_project(owner=_owner(request))}


@app.get("/api/studio")
def studio_list(request: Request):
    """이 클라이언트(IP)가 만든 작품 목록 — 작품 관리 페이지의 카드용 요약."""
    return {"projects": studio.list_projects(_owner(request))}


@app.get("/api/studio/{project_id}")
def studio_get(project_id: str):
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    # 발행 영상의 로컬 파일 경로(내부 정보)는 프론트에 노출하지 않는다 — id로만 스트리밍.
    project["published"] = [{k: v for k, v in e.items() if k != "path"}
                            for e in project.get("published", [])]
    return project


@app.delete("/api/studio/{project_id}")
def studio_delete(project_id: str, request: Request):
    if not studio.delete_project(project_id, _owner(request)):
        raise HTTPException(404, "프로젝트를 찾을 수 없거나 삭제 권한이 없어요.")
    return {"ok": True}


class PublishRequest(BaseModel):
    job_id: str
    episode_num: int = 1
    title: str | None = None


@app.post("/api/studio/{project_id}/publish")
def studio_publish(project_id: str, req: PublishRequest):
    """완성된 영상(job의 결과 mp4)을 프로젝트 '발행된 영상' 목록에 저장한다."""
    job = jobs.get(req.job_id)
    if not job or not job.get("video_path"):
        raise HTTPException(400, "저장할 영상을 찾을 수 없어요.")
    entry = studio.add_published(project_id, episode_num=req.episode_num,
                                 path=job["video_path"], title=req.title)
    if entry is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    return entry


@app.get("/api/studio/{project_id}/published/{vid}/video")
def studio_published_video(project_id: str, vid: str):
    path = studio.get_published_path(project_id, vid)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "영상 파일을 찾을 수 없어요.")
    return FileResponse(path, media_type="video/mp4")


@app.delete("/api/studio/{project_id}/published/{vid}")
def studio_delete_published(project_id: str, vid: str):
    if not studio.delete_published(project_id, vid):
        raise HTTPException(404, "발행된 영상을 찾을 수 없어요.")
    return {"ok": True}


class ProjectUpdateRequest(BaseModel):
    title: str | None = None
    logline: str | None = None
    synopsis: str | None = None
    characters: list[CharacterModel] | None = None  # 온보딩 B: 스튜디오 이동 시 편집분(인물 이미지 포함) 반영
    key_scene: KeySceneModel | None = None


@app.patch("/api/studio/{project_id}")
def studio_update(project_id: str, req: ProjectUpdateRequest):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    project = studio.update_project(project_id, **fields)
    if project is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    return project


class GenerateNoteRequest(BaseModel):
    note: str = ""  # 사용자가 생성 시점에 남기는 의견/요청(선택) — 프롬프트에 그대로 반영


@app.post("/api/studio/{project_id}/generate-synopsis")
def studio_generate_synopsis(project_id: str, req: GenerateNoteRequest = GenerateNoteRequest()):
    """전체 줄거리를 AI로 (재)생성 — 로그라인+등장인물 기반. 저장 후 프로젝트 반환."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    idea = project.get("idea") or project.get("logline", "")
    try:
        synopsis = generate_synopsis(idea, project.get("logline", ""),
                                     project.get("characters", []), note=req.note)
    except Exception as e:
        raise HTTPException(502, f"전체 줄거리 생성에 실패했어요. 잠시 후 다시 시도해주세요. ({e})")
    if looks_like_clarification(synopsis):
        raise HTTPException(422, f"AI가 의견 내용을 확인해달라고 해요 — 의견을 더 명확하게 적어주세요.\n\n{synopsis}")
    studio.update_project(project_id, synopsis=synopsis)
    return studio.get_project(project_id)


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


class AutofitRequest(BaseModel):
    mode: str = "compress"   # compress | expand | split


@app.post("/api/studio/{project_id}/episodes/{num}/measure-duration")
def studio_measure_duration(project_id: str, num: int):
    """[확정] 게이트 1단계 — 대본으로 스켈레톤을 만들어 화 전체 러닝타임을 재고 90~120초
    게이트를 판정한다(상세콘티·이미지·영상은 만들지 않음, LLM 1회). 반환: duration_gate.measure()."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    if not (episode.get("script") or "").strip():
        raise HTTPException(400, "대본이 먼저 있어야 분량을 잴 수 있어요.")
    # ★2026-07-23: 대본이 안 바뀌었으면(뼈대 지문 일치) 캐시된 v3_skeleton으로 LLM 없이 즉시 측정
    # — 매 클릭 재생성으로 느린 터널에서 60초+ 걸려 "측정 실패"로 보이던 문제. 새로 만들면 저장해
    # 이후 [드라마 만들기](produce)도 그대로 재사용한다.
    script = episode["script"]
    sig = _script_signature(script)
    cached = episode.get("v3_skeleton") if episode.get("v3_skeleton_src") == sig else None
    try:
        result = duration_gate.measure(script, episode=num,
                                       characters=project.get("characters") or [],
                                       skeleton_text=cached)
    except Exception as e:
        # 원시 500은 CORS 헤더가 안 붙어 브라우저에 "Failed to fetch"로만 보인다 — 감싸서 전달.
        raise HTTPException(500, f"분량 측정에 실패했어요: {e}")
    if not cached:
        studio.update_episode(project_id, num, v3_skeleton=result["skeleton"],
                              v3_skeleton_src=sig, v3_scenes=[])
    return result


@app.post("/api/studio/{project_id}/episodes/{num}/autofit-duration")
def studio_autofit_duration(project_id: str, num: int, req: AutofitRequest):
    """[확정] 게이트 2단계 — 대본을 AI로 재작성(compress/expand/split)해 목표 분량에 맞추고
    재측정한다. 조정된 대본을 episode.script에 저장(사용자가 명시적으로 요청한 조정)."""
    if req.mode not in ("compress", "expand", "split"):
        raise HTTPException(400, "mode는 compress|expand|split 중 하나예요.")
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    if not (episode.get("script") or "").strip():
        raise HTTPException(400, "대본이 먼저 있어야 맞출 수 있어요.")
    result = duration_gate.autofit(episode["script"], mode=req.mode, episode=num,
                                   characters=project.get("characters") or [])
    studio.update_episode(project_id, num, script=result["script"])
    return result


@app.post("/api/studio/{project_id}/episodes/{num}/generate-script")
def studio_generate_script(project_id: str, num: int, req: GenerateNoteRequest = GenerateNoteRequest()):
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
    # 요약이 없으면 전체 줄거리에서 이번 화 개요를 먼저 만든다. 그 개요를 포함한 전체 작품
    # 바이블(모든 캐릭터·이전 화 요약·직전 엔딩)을 조립한 뒤 대본을 생성한다.
    summary = episode.get("summary") or ""
    if not summary:
        summary = generate_episode_plan_summary(
            num, project.get("logline", ""), project.get("synopsis", ""),
            project.get("characters", []))
        if looks_like_clarification(summary):
            raise HTTPException(422, f"AI가 화 요약을 만들기 전에 확인이 필요하다고 해요.\n\n{summary}")
        studio.update_episode(project_id, num, summary=summary)
        episode["summary"] = summary
        project["episodes"] = [episode if ep.get("num") == num else ep
                               for ep in project.get("episodes", [])]
    bible = studio_script_bible(project, num)
    script = generate_script(idea, project["logline"], episode=num, characters=chars,
                             summary=summary, note=req.note, bible=bible)
    if looks_like_clarification(script):
        raise HTTPException(422, f"AI가 의견 내용을 확인해달라고 해요 — 의견을 더 명확하게 적어주세요.\n\n{script}")
    studio.update_episode(project_id, num, script=script)
    return studio.get_episode(project_id, num)


@app.post("/api/studio/{project_id}/episodes/{num}/generate-summary")
def studio_generate_summary(project_id: str, num: int, req: GenerateNoteRequest = GenerateNoteRequest()):
    """이 화 요약을 AI로 (재)생성. 대본이 이미 있으면 대본을 요약하고, 아직 없으면 전체
    줄거리에서 '이번 화 사건'을 뽑아 요약을 먼저 만든다(요약 먼저 → 대본 흐름)."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    try:
        if episode.get("script"):
            summary = generate_episode_summary(episode["script"], note=req.note)
        else:
            summary = generate_episode_plan_summary(
                num, project.get("logline", ""), project.get("synopsis", ""),
                project.get("characters", []), note=req.note)
    except Exception as e:
        raise HTTPException(502, f"요약 생성에 실패했어요. 잠시 후 다시 시도해주세요. ({e})")
    if looks_like_clarification(summary):
        raise HTTPException(422, f"AI가 의견 내용을 확인해달라고 해요 — 의견을 더 명확하게 적어주세요.\n\n{summary}")
    studio.update_episode(project_id, num, summary=summary)
    return studio.get_episode(project_id, num)


def _episode_save_fn(project_id: str, num: int):
    """백그라운드 job이 생성한 씬·샷·스틸을 그 화에 저장하는 콜백(orchestrator는 studio를
    직접 import 못 함 — 순환참조 — 이라 서버가 콜백을 주입)."""
    return lambda **fields: studio.update_episode(project_id, num, **fields)


def _run_with_locked_references(build_fn, project_id: str, num: int, job_id: str):
    """스틸/영상 생성 스레드의 실제 진입점 — 본 작업(build_fn) 전에 캐릭터 얼굴 고정값을
    확정한다(studio.lock_character_references). 초상화 없는 캐릭터가 있으면 그 자리에서
    만들어 채우므로, 이 단계에서 시간이 좀 걸릴 수 있다(사용자에게는 이후 stage 메시지로만
    보임 — 인물 일관성 관리는 여전히 비가시적으로 처리)."""
    jobs.update(job_id, stage="인물 기준 이미지 준비 중")
    project = studio.lock_character_references(project_id)
    if project is None:
        jobs.update(job_id, status="error", stage="오류", error="프로젝트를 찾을 수 없어요.")
        return
    episode = studio.get_episode(project_id, num)
    if episode is None:
        jobs.update(job_id, status="error", stage="오류", error="화를 찾을 수 없어요.")
        return
    build_fn(project, episode, job_id, _episode_save_fn(project_id, num))


@app.post("/api/studio/{project_id}/episodes/{num}/preview-stills")
def studio_preview_stills(project_id: str, num: int, scene_num: int = 1):
    """영상 만들기 전 미리보기 — ★2026-07-21(사용자 지시): 한 번에 전체 씬이 아니라 scene_num
    씬 하나만 만드는 백그라운드 job(기본 1씬). 완료되면 화의 scene_stills에 누적 저장된다
    (프론트는 job 완료 후 프로젝트를 다시 받아 스틸을 보여주고, "다음 씬 만들기" 버튼으로
    scene_num을 올려가며 다시 호출한다)."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    if not episode.get("script"):
        raise HTTPException(400, "대본이 먼저 있어야 장면 미리보기를 만들 수 있어요.")
    job_id = jobs.create()
    threading.Thread(target=_run_with_locked_references,
                     args=(partial(generate_stills_for_scene, scene_num=scene_num),
                          project_id, num, job_id),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/studio/{project_id}/episodes/{num}/cuts/{scene_num}/{cut_num}/regenerate")
def studio_regenerate_cut(project_id: str, num: int, scene_num: int, cut_num: int):
    """미리보기의 특정 컷 이미지만 다시 생성(동기 — 이미지 1장이라 몇 초~수십 초). 갱신된
    scene_stills를 화에 저장하고 그 컷 항목을 반환한다."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    try:
        new_still = regenerate_cut_still(project, episode, scene_num, cut_num)
    except Exception as e:
        raise HTTPException(502, f"이미지 재생성에 실패했어요. ({e})")
    stills = [s for s in (episode.get("scene_stills") or [])
             if not (s.get("scene_num") == scene_num and s.get("cut_num") == cut_num)]
    stills.append(new_still)
    stills.sort(key=lambda s: (s.get("scene_num", 0), s.get("cut_num", 0)))
    studio.update_episode(project_id, num, scene_stills=stills)
    return new_still


@app.delete("/api/studio/{project_id}/episodes/{num}/cuts/{scene_num}/{cut_num}")
def studio_delete_cut(project_id: str, num: int, scene_num: int, cut_num: int):
    """미리보기의 특정 컷을 삭제(메타데이터만 — 이미 생성된 이미지/영상 파일은 지우지 않는다).
    scene_stills에서 빼고, v3.1 모드로 만든 컷이면 v3_scenes[].stills에서도 같이 빼서 다음
    미리보기/제작 때 삭제된 컷이 되살아나지 않게 한다. 그 씬의 컷을 전부 지우면 다음 "씬 만들기"
    로 그 씬을 다시 만들 수 있다(scene_stills에 그 씬 항목이 없으면 미완성 씬으로 간주됨)."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")

    def _not_target(s):
        return not (s.get("scene_num") == scene_num and s.get("cut_num") == cut_num)

    stills = [s for s in (episode.get("scene_stills") or []) if _not_target(s)]
    fields = {"scene_stills": stills}

    v3_scenes = episode.get("v3_scenes") or []
    if v3_scenes:
        new_v3_scenes = []
        for s in v3_scenes:
            if int(s.get("scene_num") or -1) == scene_num and s.get("stills"):
                s = {**s, "stills": [st for st in s["stills"] if _not_target(st)]}
            new_v3_scenes.append(s)
        fields["v3_scenes"] = new_v3_scenes

    studio.update_episode(project_id, num, **fields)
    return {"ok": True}


@app.post("/api/studio/{project_id}/episodes/{num}/cuts/{scene_num}/{cut_num}/videoize")
def studio_videoize_cut(project_id: str, num: int, scene_num: int, cut_num: int, note: str = ""):
    """미리보기(또는 재생성)로 만들어둔 특정 컷 스틸을 그대로 영상화 — 백그라운드 job.
    note: '다시 영상화' 시 사용자가 입력한 의견(모션 프롬프트에 최우선 지시로 반영).
    프론트가 /api/jobs/{id} 폴링으로 완료·영상 URL을 받는다(기존 job 메커니즘 재사용).
    완료 시 영상 경로를 그 컷 스틸에 저장해 페이지를 넘겨도·재로드해도 영상이 유지된다."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    job_id = jobs.create()
    threading.Thread(
        target=_run_with_locked_references,
        args=(partial(videoize_cut_job, scene_num=scene_num, cut_num=cut_num, note=note),
              project_id, num, job_id),
        daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/studio/{project_id}/episodes/{num}/cuts/{scene_num}/{cut_num}/video")
def studio_get_cut_video(project_id: str, num: int, scene_num: int, cut_num: int):
    """그 컷 스틸에 저장된 영상 파일을 서빙(영상화 결과가 페이지 넘겨도·재로드해도 유지되게).
    job 기반 /api/jobs/{id}/video와 달리 화 데이터(scene_stills[].video_path)에서 읽는다."""
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    still = next((s for s in (episode.get("scene_stills") or [])
                 if s.get("scene_num") == scene_num and s.get("cut_num") == cut_num and s.get("video_path")),
                None)
    if not still or not os.path.exists(still["video_path"]):
        raise HTTPException(404, "이 컷의 영상이 아직 없어요.")
    return FileResponse(still["video_path"], media_type="video/mp4")


@app.post("/api/studio/{project_id}/episodes/{num}/produce")
def studio_produce_episode(project_id: str, num: int):
    """이 화를 영상으로 제작(대본→...→합본). 시간이 걸리는 작업이라 백그라운드 job으로 돌리고
    job_id를 반환 — 프론트가 /api/jobs/{id} 폴링으로 진행상황·결과 영상을 받는다."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    if not episode.get("script"):
        raise HTTPException(400, "대본이 먼저 있어야 영상을 만들 수 있어요. (대본 AI 생성 후 다시 시도)")
    job_id = jobs.create()
    threading.Thread(target=_run_with_locked_references,
                     args=(produce_episode_video, project_id, num, job_id),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/studio/{project_id}/episodes/{num}/v3/preview-scene")
def studio_preview_scene_v3(project_id: str, num: int, scene_num: int = 1):
    """v3.1 엔진 미리보기 — scene_num 씬 하나의 클립별 대표 스틸까지 만드는 백그라운드 job.
    구 preview-stills와 같은 job/stills 메커니즘(scene_stills 형태)이라 프런트가 그대로 렌더한다."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    if not episode.get("script"):
        raise HTTPException(400, "대본이 먼저 있어야 미리보기를 만들 수 있어요.")
    job_id = jobs.create()
    threading.Thread(target=_run_with_locked_references,
                     args=(partial(preview_scene_v3, scene_num=scene_num),
                          project_id, num, job_id),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/studio/{project_id}/episodes/{num}/v3/produce")
def studio_produce_v3(project_id: str, num: int):
    """v3.1 엔진 화 전체 제작 — 뼈대→씬 순차(상세블록·레퍼런스·클립 스틸·멀티샷 영상)→합본까지
    한 job으로. 완료 씬은 재개 시 건너뛴다. 프런트는 job 폴링으로 최종 합본 영상을 받는다."""
    project = studio.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트를 찾을 수 없어요.")
    episode = studio.get_episode(project_id, num)
    if not episode:
        raise HTTPException(404, "화를 찾을 수 없어요.")
    if not episode.get("script"):
        raise HTTPException(400, "대본이 먼저 있어야 영상을 만들 수 있어요.")
    job_id = jobs.create()
    threading.Thread(target=_run_with_locked_references,
                     args=(produce_episode_v3_job, project_id, num, job_id),
                     daemon=True).start()
    return {"job_id": job_id}


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
        plan_summary = episode.get("summary") or generate_episode_plan_summary(
            num, project.get("logline", ""), project.get("synopsis", ""),
            project.get("characters", []))
        if looks_like_clarification(plan_summary):
            raise HTTPException(422, f"AI가 화 요약을 만들기 전에 확인이 필요하다고 해요.\n\n{plan_summary}")
        episode["summary"] = plan_summary
        project["episodes"] = [episode if ep.get("num") == num else ep
                               for ep in project.get("episodes", [])]
        bible = studio_script_bible(project, num)
        script = generate_script(idea, project["logline"], episode=num,
                                 characters=project["characters"], summary=plan_summary, bible=bible)
        try:
            summary = generate_episode_summary(script)
        except Exception:
            # 대본 사후 요약이 실패해도 생성 전에 만든 화 개요는 보존한다.
            summary = plan_summary
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
                                    characters=project["characters"], work=project["work"],
                                    prior_conti=episode.get("conti_full") or "")
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
    if "total" in job:
        resp["total"] = job["total"]
    if "stills" in job:
        resp["stills"] = job["stills"]
    return resp


@app.get("/api/jobs/{job_id}/video")
def get_video(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.get("video_path"):
        raise HTTPException(404, "아직 영상이 없어요.")
    return FileResponse(job["video_path"], media_type="video/mp4")


# 로컬에서 프론트까지 한 번에 확인하고 싶을 때용(배포 시엔 GitHub Pages/Vercel이 이 폴더를 대신 서빙).
class _NoCacheStaticFiles(StaticFiles):
    """★2026-07-22: 기본 StaticFiles 응답엔 Cache-Control이 없어서, ETag/Last-Modified만으로
    브라우저가 재검증(304) 없이 그냥 디스크/메모리 캐시를 그대로 써버리는 경우가 실측으로
    확인됨(app.js를 고쳐도 새로고침해도 반영 안 되는 문제) — 매 응답에 no-cache를 강제해 항상
    서버에 재검증하게 한다(ETag가 같으면 304라 실제 전송 비용은 거의 없음)."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", _NoCacheStaticFiles(directory="static", html=True), name="static")
