# -*- coding: utf-8 -*-
"""온보딩 이후 프로덕션 워크스페이스("스튜디오")의 프로젝트 상태. jobs.py/chat.py와 같은
in-memory 저장 패턴(스레드 락) — DB 구성은 이번 단계에서 미루고, 나중에 옮길 때 이 모듈만
갈아끼우면 되게 저장 로직을 여기 몰아둔다."""
import base64
import concurrent.futures
import json
import re
import threading
import time
import uuid
from pathlib import Path

import vendor.storyboard.bot.openrouter_image as oi

from pipeline import project_setup
from pipeline.orchestrator import generate_character_portrait, generate_synopsis, _make_face_reference

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


def _touch(p: dict) -> None:
    """프로젝트의 수정 시각을 갱신(_LOCK 안에서 호출). 작품 목록 정렬용."""
    p["updated_at"] = time.time()


def _project_title(p: dict) -> str:
    """카드에 보여줄 제목 — 명시적 title이 있으면 그걸, 없으면 로그라인/아이디어에서 유도."""
    t = (p.get("title") or "").strip()
    if t:
        return t
    return (p.get("logline") or p.get("idea") or "제목 없는 작품").strip()


def _project_stage(p: dict) -> str:
    """프로젝트 전체 진행상태 — 화들 중 가장 앞선 단계를 대표값으로."""
    eps = p.get("episodes") or []
    best = -1
    for ep in eps:
        try:
            best = max(best, STAGE_ORDER.index(ep.get("stage")))
        except ValueError:
            best = max(best, 0)
    return STAGE_ORDER[best] if best >= 0 else STAGE_ORDER[0]


def _summary(project_id: str, p: dict) -> dict:
    return {
        "id": project_id,
        "title": _project_title(p),
        "stage": _project_stage(p),
        "episode_count": len(p.get("episodes") or []),
        "updated_at": p.get("updated_at", 0),
    }


def list_projects(owner: str) -> list[dict]:
    """이 owner(IP)가 만든 작품 요약 목록 — 최근 수정순."""
    with _LOCK:
        items = [_summary(pid, p) for pid, p in _PROJECTS.items()
                 if p.get("owner") == owner]
    items.sort(key=lambda s: s["updated_at"], reverse=True)
    return items


def delete_project(project_id: str, owner: str) -> bool:
    """소유자 확인 후 프로젝트 삭제. 소유자가 다르거나 없으면 False."""
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p or p.get("owner") != owner:
            return False
        del _PROJECTS[project_id]
        _save()
        return True


def add_published(project_id: str, *, episode_num: int, path: str,
                  title: str | None = None) -> dict | None:
    """완성된 영상을 프로젝트의 '발행된 영상' 목록에 저장(파일 경로 + 메타). 파일 자체는
    이미 디스크에 있으니 경로만 기록하고, 서빙은 server.py가 FileResponse로 처리한다."""
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        entry = {
            "id": uuid.uuid4().hex,
            "episode_num": episode_num,
            "title": title or f"{episode_num}화",
            "path": path,
            "created_at": time.time(),
        }
        p.setdefault("published", []).append(entry)
        _touch(p)
        _save()
        # 프론트에 파일 경로(내부 정보)는 굳이 내보내지 않는다.
        return {k: v for k, v in entry.items() if k != "path"}


def get_published_path(project_id: str, vid: str) -> str | None:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        for e in p.get("published", []):
            if e["id"] == vid:
                return e.get("path")
    return None


def delete_published(project_id: str, vid: str) -> bool:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return False
        pubs = p.get("published", [])
        after = [e for e in pubs if e["id"] != vid]
        if len(after) == len(pubs):
            return False
        p["published"] = after
        _touch(p)
        _save()
        return True

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
        return None
    name = character.get("name") or "인물"
    b64 = image.split(",", 1)[1]
    original_png = base64.b64decode(b64)
    # ★2026-07-22: 인물을 person으로 등록(id 획득). 카드 원본 이미지는 <id>_원본.png로 보관하고,
    # 그 원본에서 '얼굴 전용 레퍼런스'(정면·얼굴~어깨 크롭·중립 상의·액세서리 제거·흰 배경)를
    # 만들어 대표 <id>.png로 쓴다(같은 요소 id로 원본↔얼굴레퍼런스 매칭). 중립화 실패 시 원본 폴백.
    # ★2026-07-23: register_element는 person 우선 가드가 있어, 콘티 추출이 costume으로 강등해뒀던
    # 동명 요소도 여기서 person으로 되돌린다. 반환한 elem.id를 호출부(lock_character_references)가
    # 캐릭터의 element_id로 연결한다.
    elem = oi.register_element(work, name, etype="person", aliases=[name])
    # ★2026-07-22: 이미 얼굴 레퍼런스가 있으면 매번 다시 만들지 않는다(재실행마다 img2img 중립화가
    # 반복돼 '인물 기준 이미지 준비 중'이 오래 걸리던 문제). 처음 한 번만 원본 보관 + 중립화.
    if oi.element_has_image(work, elem):
        return elem
    oi.save_element_image(work, elem, original_png, variant="원본")
    face_png = _make_face_reference(original_png, character) or original_png
    oi.save_element_image(work, elem, face_png)
    gender_en = _GENDER_EN.get((character.get("gender") or "").strip())
    if gender_en:
        with oi._ELEMENTS_LOCK:
            elems = oi.load_elements(work)
            for e in elems:
                if e.get("id") == elem.get("id"):
                    e["gender"] = gender_en
                    break
            oi._save_elements(work, elems)
    return elem


def lock_character_references(project_id: str) -> dict | None:
    """스틸 생성으로 넘어가는 시점에 한 번만 호출 — 이 시점의 캐릭터 이미지를 얼굴 고정값으로
    등록한다(요소 레지스트리, oi.shot_refs가 이걸로 얼굴 참조). 사용자가 "사진 다시 생성"을
    몇 번 누르든 그때는 화면용 이미지만 바뀌고 고정값은 그대로였다가, 여기서 그 순간의
    최종 이미지가 이후 모든 컷의 얼굴 기준으로 굳는다.

    ★실사용 리포트: 스튜디오에서 '+ 캐릭터 추가'로 만든 캐릭터나 더미 데이터 캐릭터는 애초에
    image가 없는데, 예전엔 이럴 때 등록 자체가 조용히 스킵돼(에러도 없이) 컷마다 그 인물의
    얼굴이 다르게 나왔다 — 여기서는 이미지가 없는 캐릭터에 한해 그 자리에서 초상화를 만들어
    채운 뒤 등록한다(이미지가 이미 있으면 새로 생성하지 않고 그 이미지 그대로 고정)."""
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        work = p["work"]
        characters = list(p["characters"])
        missing = [c for c in characters
                  if not (c.get("image") or "").startswith("data:image")]

    if missing:
        def _one(ch):
            try:
                png = generate_character_portrait(ch)
                return ch["id"], "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            except Exception:
                return ch["id"], None

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(missing), 4)) as ex:
            results = list(ex.map(_one, missing))

        with _LOCK:
            p = _PROJECTS.get(project_id)
            if not p:
                return None
            by_id = {c["id"]: c for c in p["characters"]}
            for char_id, image in results:
                ch = by_id.get(char_id)
                if ch and image:
                    ch["image"] = image
            characters = list(p["characters"])
            _touch(p)
            _save()

    # ★2026-07-23: 얼굴 참조 등록 후, 각 캐릭터를 그 person 엘리먼트에 연결(element_id) — 이전엔
    # element_id=None이라 인물↔요소가 끊겨 얼굴 앵커가 약했다. 이제 id를 캐릭터에 박아 저장한다.
    elem_ids: dict[str, str] = {}
    for ch in characters:
        if (ch.get("image") or "").startswith("data:image"):
            try:
                elem = _save_character_reference(work, ch)
                if elem and elem.get("id"):
                    elem_ids[ch["id"]] = elem["id"]
            except Exception:
                pass
    if elem_ids:
        with _LOCK:
            p = _PROJECTS.get(project_id)
            if p:
                for c in p["characters"]:
                    if c.get("id") in elem_ids:
                        c["element_id"] = elem_ids[c["id"]]
                _touch(p)
                _save()

    return get_project(project_id)


_DEMO_SYNOPSIS = (
    "태민은 대기업 그룹 회장의 손자이지만, 후계 경쟁과 정략결혼 압박에서 벗어나고 싶어 신분을 "
    "숨긴 채 도시 외곽의 작은 편의점에서 야간 알바를 시작한다. 그곳에서 그는 아버지의 빚 때문에 "
    "밤낮으로 일하는 편의점 알바생 수진을 만난다. 처음엔 데면데면하던 둘은 매일 밤 같은 공간에서 "
    "부딪히며 조금씩 가까워진다.\n\n"
    "태민은 수진의 아버지가 사채에 시달린다는 걸 알게 되고, 자신의 정체를 숨긴 채 그녀를 돕기 "
    "위해 '한 달간 연인인 척하는 계약연애'를 제안한다. 계약이라는 명분 아래 시작된 관계지만, "
    "함께 시간을 보낼수록 둘 다 진짜 감정에 흔들린다.\n\n"
    "태민의 가족이 그의 행방을 추적하며 정체가 드러날 위기가 닥치고, 수진은 자신이 이용당한 게 "
    "아닌지 의심한다. 결국 태민은 모든 것을 걸고 진심을 고백하고, 두 사람은 신분과 계약을 넘어 "
    "진짜 사랑으로 다시 시작하기로 한다."
)

_DEMO_SCRIPT_1 = (
    "1. 편의점 계산대 / 밤 11시\n\n"
    "수진이 진열대를 정리하고 있다. 야근의 피로가 얼굴에 묻어난다.\n"
    "자동문이 열리고 태민이 들어온다. 깔끔한 옷차림, 어딘가 이 공간과 어울리지 않는다.\n\n"
    "수진: 어서오세요.\n"
    "태민: (진열대는 보지 않고 수진을 본다) 이 시간에 혼자예요?\n"
    "수진: (경계하며) …무슨 일이시죠?\n\n"
    "2. 편의점 계산대 / 밤 11시 10분\n\n"
    "태민이 카드를 내밀다 멈춘다.\n\n"
    "태민: 한 달만. 내 연인인 척해줘요. 대가는 충분히 치를게요.\n"
    "수진: (손이 굳는다) …지금 무슨 말을 하는 거예요?\n"
    "태민: 손해 볼 건 없을 텐데. 생각해봐요.\n\n"
    "수진, 태민이 두고 간 명함을 내려다본다. 화면 어두워진다."
)


def _seed_characters() -> list[dict]:
    return [
        {
            "id": uuid.uuid4().hex,
            "name": "강태민", "gender": "남", "age": "28",
            "role": "대기업 그룹 회장 손자(신분을 숨긴 편의점 알바)",
            "line": "한 달만. 내 연인인 척해줘요.",
            "appearance": "큰 키에 균형 잡힌 체형, 짧게 정리한 검은 머리, 서늘하고 날카로운 눈매.",
            "description": "후계 경쟁과 정략결혼 압박에서 벗어나려 신분을 숨겼다. 차갑고 무심해 보이지만 속은 외롭고 여리다.",
        },
        {
            "id": uuid.uuid4().hex,
            "name": "이수진", "gender": "여", "age": "25",
            "role": "편의점 야간 알바생",
            "line": "이거 다 연기인 거, 알아요.",
            "appearance": "긴 갈색 웨이브 머리, 크고 동그란 눈매, 편의점 유니폼 차림.",
            "description": "아버지의 빚 때문에 밤낮으로 일한다. 씩씩하고 자존심이 강해 쉽게 기대지 않는다.",
        },
    ]


def seed_demo_project(owner: str = "") -> str:
    """테스트용 더미 프로젝트를 즉시 생성(AI 호출 없이 하드코딩 내용) — 로그라인·전체 줄거리·
    캐릭터 2명(전 필드)·1화(부제목/요약/대본)까지 채워둔다. 매번 새로 채우는 수고 없이 바로
    스튜디오·영상 제작 흐름을 테스트할 수 있게 한다."""
    project_id = uuid.uuid4().hex
    work = f"studio-{project_id[:8]}"
    project_setup.ensure_project(work)
    characters = _seed_characters()
    ep = _new_episode(1)
    ep.update({
        "subtitle": "위험한 첫 만남",
        "character_ids": [c["id"] for c in characters],
        "summary": ("신분을 숨긴 재벌가 손자 태민이 야간 편의점에서 알바생 수진을 만난다. "
                    "태민은 수진에게 '한 달간 연인인 척해달라'는 수상한 계약을 제안하고, "
                    "수진은 거절과 수락 사이에서 흔들리며 화가 끝난다."),
        "script": _DEMO_SCRIPT_1,
        "stage": "대본 완료",
    })
    now = time.time()
    with _LOCK:
        _PROJECTS[project_id] = {
            "work": work,
            "owner": owner,
            "title": "재벌 계약연애 (데모)",
            "idea": "재벌가 손자가 신분을 숨기고 편의점 알바를 하다 사장 딸과 계약연애를 하게 된다",
            "logline": "신분을 숨긴 재벌가 손자와 편의점 알바생의 위험한 계약연애.",
            "synopsis": _DEMO_SYNOPSIS,
            "characters": characters,
            "key_scene": None,
            "episodes": [ep],
            "published": [],
            "created_at": now,
            "updated_at": now,
        }
        _save()
    return project_id


def create_project(idea: str, card: dict, owner: str = "") -> str:
    """온보딩 카드(로그라인+characters[+key_scene])로 새 프로젝트를 만든다.
    1화를 빈 상태로 자동 생성해둔다. 캐릭터 얼굴 고정값 등록은 여기서 하지 않는다 —
    온보딩에서 만든 초상화도 이후 캐릭터 화면에서 얼마든지 다시 생성될 수 있으므로,
    lock_character_references가 스틸 생성 시점에 그때의 최종 이미지로 한 번에 처리한다."""
    project_id = uuid.uuid4().hex
    work = f"studio-{project_id[:8]}"
    project_setup.ensure_project(work)

    characters = card.get("characters", [])
    for ch in characters:
        ch.setdefault("id", uuid.uuid4().hex)

    logline = card.get("logline", "")
    if not idea and not logline and not characters:
        synopsis = ""  # 온보딩 건너뛰고 만든 빈 프로젝트 — 채울 내용이 없으니 LLM 호출 자체를 스킵
    else:
        try:
            synopsis = generate_synopsis(idea, logline, characters)
        except Exception:
            synopsis = ""  # 실패해도 프로젝트 생성 자체는 막지 않음 — 화면에서 빈 상태로 보임

    now = time.time()
    with _LOCK:
        _PROJECTS[project_id] = {
            "work": work,
            "owner": owner,
            "title": card.get("title") or "",  # 비면 로그라인/아이디어에서 유도
            "idea": idea,
            "logline": logline,
            "synopsis": synopsis,
            "characters": characters,
            "key_scene": card.get("key_scene"),
            "episodes": [_new_episode(1)],
            "published": [],
            "created_at": now,
            "updated_at": now,
        }
        _save()
    return project_id


def update_project(project_id: str, **fields) -> dict | None:
    """프로젝트 상위 필드(제목·로그라인·전체 줄거리 등) 수정. 허용된 키만 반영한다."""
    allowed = {"title", "logline", "synopsis"}
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        for k, v in fields.items():
            if k in allowed:
                p[k] = v
        _touch(p)
        _save()
        return dict(p)


def add_character(project_id: str, character: dict) -> dict | None:
    """캐릭터 카드 추가. 얼굴 고정값 등록은 여기서 하지 않는다(lock_character_references가
    스틸 생성 시점에 한 번에 처리 — update_character 주석 참고)."""
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        character = dict(character)
        character["id"] = uuid.uuid4().hex
        p["characters"].append(character)
        _touch(p)
        _save()
        return dict(character)


def update_character(project_id: str, char_id: str, **fields) -> dict | None:
    """캐릭터 필드 수정(이름·역할·대사·이미지). ★사진을 "다시 생성"해서 저장하는 시점에는
    고정값(요소 레지스트리 얼굴 참조)을 만들지 않는다 — 사용자가 마음에 드는 사진을 몇 번이고
    다시 뽑아볼 수 있게 여기서는 그냥 화면에 보일 이미지만 저장한다. 실제 씬 일관성에 쓰일
    고정값은 스틸 생성으로 넘어가는 시점에 그때의 최종 이미지를 기준으로 한 번에 만든다
    (lock_character_references 참고)."""
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return None
        ch = next((c for c in p["characters"] if c.get("id") == char_id), None)
        if not ch:
            return None
        ch.update(fields)
        updated = dict(ch)
        _touch(p)
        _save()
    return updated


def delete_character(project_id: str, char_id: str) -> bool:
    with _LOCK:
        p = _PROJECTS.get(project_id)
        if not p:
            return False
        before = len(p["characters"])
        p["characters"] = [c for c in p["characters"] if c.get("id") != char_id]
        _touch(p)
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
        "scene_lines": None,  # [[씬번호, 씬설계 한 줄], ...] — 미리보기를 씬 하나씩 처리할 때 씀
        "conti_full": None,
        "scenes": None,
        "shots_by_scene": None,
        "scene_stills": None,  # 영상 만들기 전 씬별 대표 스틸컷 미리보기
        "cut_results": None,
        "compiled_path": None,
        # ── v3.1 파이프라인(scene→clip→block) 전용 상태 — 구 shot 필드와 별도 네임스페이스로
        # 저장해 기존 프로젝트를 깨뜨리지 않는다(11단계 안전 폴백). 옛 화는 이 필드가 없어도
        # 잡 래퍼가 .get()으로 안전하게 처리하고, v3.1로 처음 제작할 때 채워진다.
        "v3_skeleton": None,   # 화 전체 뼈대 원문(3단계) — 씬별 상세블록 생성의 입력
        "v3_scenes": None,     # [{scene_num, state, conti_text, handoff, stills, plan}] — 씬별 완료 상태·재개용
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
        _touch(p)
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
                _touch(p)
                _save()
                return
