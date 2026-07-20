# -*- coding: utf-8 -*-
"""온보딩 이후 프로덕션 워크스페이스("스튜디오")의 프로젝트 상태. jobs.py/chat.py와 같은
in-memory 저장 패턴(스레드 락) — DB 구성은 이번 단계에서 미루고, 나중에 옮길 때 이 모듈만
갈아끼우면 되게 저장 로직을 여기 몰아둔다."""
import base64
import json
import re
import threading
import uuid
from pathlib import Path

import vendor.storyboard.bot.openrouter_image as oi

from pipeline import project_setup
from pipeline.orchestrator import generate_synopsis

_LOCK = threading.Lock()
_PROJECTS: dict[str, dict] = {}

# 프로젝트를 디스크(JSON)에 저장해 서버 재시작에도 살아남게 한다 — DB는 아직 미루되, 인메모리만
# 쓰면 재시작 때마다 프로젝트가 날아가 "저장 실패(404)"가 나서(실사용 리포트) 최소한의 영속성을
# 여기 둔다. 나중에 DB로 옮길 때 이 _load/_save만 갈아끼우면 된다.
_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "studio.json"


def _save() -> None:
    """_LOCK을 이미 잡은 상태에서 호출한다. 전체 스냅샷을 통째로 덤프(프로젝트 수가 적은 데모라 충분)."""
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_PROJECTS, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STORE_PATH)  # 원자적 교체 — 쓰는 도중 크래시해도 기존 파일이 안 깨짐
    except Exception:
        pass  # 저장 실패해도 인메모리 상태는 유지 — 영속성만 포기(요청 자체는 성공 처리)


def _load() -> None:
    if not _STORE_PATH.exists():
        return
    try:
        _PROJECTS.update(json.loads(_STORE_PATH.read_text(encoding="utf-8")) or {})
    except Exception:
        pass


_load()

_GENDER_EN = {"남": "male", "여": "female", "male": "male", "female": "female"}

STAGE_ORDER = [
    "대본 대기", "대본 완료", "씬설계 완료",
    "샷분해 완료", "이미지 완료", "영상화 완료", "합본 완료",
]
# ★"콘티"(상세 스토리보드, GPT 이미지 생성용 내부 산출물)는 사용자에게 보일 개념이 아니라서
# STAGE_ORDER에서 뺐다 — server.py의 advance는 씬설계 완료 → 샷분해 완료로 그 안에서
# 콘티 생성까지 한 번에 처리하고, conti_full/scenes는 내부적으로만 episode dict에 저장한다.


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w가-힣.\-]+", "_", name or "인물").strip("_.") or "인물"


def _save_character_reference(work: str, character: dict) -> None:
    """온보딩/캐릭터 카드의 인물 이미지(base64 data URL)를 요소 레지스트리 참조 파일로 저장하고
    등록한다 — 이후 샷 이미지 생성이 이 파일을 얼굴 참조로 찾아 쓸 수 있게(oi.element_refs).
    성별도 같이 저장해둔다(voice_for가 이미 이 필드로 성별에 맞는 TTS 보이스를 배정하므로
    —이름·성별·나이·외형이 이미지뿐 아니라 목소리에도 일관되게 이어지도록)."""
    image = character.get("image")
    if not image or not image.startswith("data:image"):
        return
    name = character.get("name") or "인물"
    b64 = image.split(",", 1)[1]
    png = base64.b64decode(b64)
    filename = f"{_safe_filename(name)}.png"
    dest_dir = oi.config.OPENROUTER_REFS_DIR / oi.canon_work(work)
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / filename).write_bytes(png)
    elem = oi.register_element(work, name, etype="person", filename=filename, aliases=[name])
    gender_en = _GENDER_EN.get((character.get("gender") or "").strip())
    if gender_en:
        with oi._ELEMENTS_LOCK:
            elems = oi.load_elements(work)
            for e in elems:
                if e.get("id") == elem.get("id"):
                    e["gender"] = gender_en
                    break
            oi._save_elements(work, elems)


def create_project(idea: str, card: dict) -> str:
    """온보딩 카드(로그라인+characters[+key_scene])로 새 프로젝트를 만든다.
    1화를 빈 상태로 자동 생성해두고, 캐릭터는 요소 레지스트리에 등록한다."""
    project_id = uuid.uuid4().hex
    work = f"studio-{project_id[:8]}"
    project_setup.ensure_project(work)

    characters = card.get("characters", [])
    for ch in characters:
        ch.setdefault("id", uuid.uuid4().hex)
        try:
            _save_character_reference(work, ch)
        except Exception:
            pass  # 참조 등록 실패해도 프로젝트 생성 자체는 막지 않음(그 캐릭터만 얼굴 불일치 리스크)

    logline = card.get("logline", "")
    if not idea and not logline and not characters:
        synopsis = ""  # 온보딩 건너뛰고 만든 빈 프로젝트 — 채울 내용이 없으니 LLM 호출 자체를 스킵
    else:
        try:
            synopsis = generate_synopsis(idea, logline, characters)
        except Exception:
            synopsis = ""  # 실패해도 프로젝트 생성 자체는 막지 않음 — 화면에서 빈 상태로 보임

    with _LOCK:
        _PROJECTS[project_id] = {
            "work": work,
            "idea": idea,
            "logline": logline,
            "synopsis": synopsis,
            "characters": characters,
            "key_scene": card.get("key_scene"),
            "episodes": [_new_episode(1)],
        }
        _save()
    return project_id


def update_project(project_id: str, **fields) -> dict | None:
    """프로젝트 상위 필드(로그라인·전체 줄거리 등) 수정. 허용된 키만 반영한다."""
    allowed = {"logline", "synopsis"}
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        for k, v in fields.items():
            if k in allowed:
                p[k] = v
        _save()
        return dict(p)


def add_character(project_id: str, character: dict) -> dict | None:
    """캐릭터 카드 추가. 이미지가 있으면 요소 레지스트리에도 등록(얼굴 참조)."""
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        character = dict(character)
        character["id"] = uuid.uuid4().hex
        work = p["work"]
    try:
        _save_character_reference(work, character)
    except Exception:
        pass
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        p["characters"].append(character)
        _save()
        return dict(character)


def update_character(project_id: str, char_id: str, **fields) -> dict | None:
    """캐릭터 필드 수정(이름·역할·대사·이미지). 이미지가 바뀌면 참조 파일도 다시 저장."""
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        ch = next((c for c in p["characters"] if c.get("id") == char_id), None)
        if not ch:
            return None
        ch.update(fields)
        work = p["work"]
        updated = dict(ch)
        _save()
    if "image" in fields:
        try:
            _save_character_reference(work, updated)
        except Exception:
            pass
    return updated


def delete_character(project_id: str, char_id: str) -> bool:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return False
        before = len(p["characters"])
        p["characters"] = [c for c in p["characters"] if c.get("id") != char_id]
        _save()
        return len(p["characters"]) != before


def _new_episode(num: int) -> dict:
    return {
        "num": num,           # 화 번호 — 고정, 수정 불가
        "subtitle": None,     # 부제목 — 사용자가 화 상세에서 편집
        "character_ids": [],  # 이 화에 등장하는 캐릭터 id(프로젝트 캐릭터 DB 참조)
        "stage": STAGE_ORDER[0],
        "script": None,
        "summary": None,
        "plan_text": None,
        "scenes_plan": None,
        "conti_full": None,
        "scenes": None,
        "shots_by_scene": None,
        "scene_stills": None,  # 영상 만들기 전 씬별 대표 스틸컷 미리보기
        "cut_results": None,
        "compiled_path": None,
    }


def get_project(project_id: str) -> dict | None:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        return dict(p) if p else None


def add_episode(project_id: str) -> dict | None:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        num = len(p["episodes"]) + 1
        ep = _new_episode(num)
        p["episodes"].append(ep)
        _save()
        return ep


def get_episode(project_id: str, num: int) -> dict | None:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        return next((dict(ep) for ep in p["episodes"] if ep["num"] == num), None)


def update_episode(project_id: str, num: int, **fields) -> None:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return
        for ep in p["episodes"]:
            if ep["num"] == num:
                ep.update(fields)
                _save()
                return
