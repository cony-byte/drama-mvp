# -*- coding: utf-8 -*-
"""v3.1 파이프라인(HANDOFF_V3_1_PIPELINE.md)의 Scene/Clip/Block 계층 스키마 + 정규화·검증 함수.

권장 구현 순서의 2단계 산출물. 3단계 이후(화 전체 1~4단계 생성기, 씬 순차 처리 오케스트레이션,
클립 단위 이미지·영상 생성, 프런트 연동)는 별도로 이어서 구현한다 — 이 모듈은 그 위에서 쓰일
순수 파싱/검증 유틸이라 오케스트레이션·API 호출을 포함하지 않는다(테스트하기 쉽게 유지).

기대하는 콘티 원문 형식은 `/Users/cony/Downloads/콘티변환규칙_v3.1.md.txt`의 "좋은 예"(392~424행)
그대로다:

    ■ 씬2 · [신혼집-안방] (sera_honeyhouse ⚠ 미등록) · 23초 · 홍차, 뺨, 그리고 목걸이
    등장: 서아(sera_female_lead / 의상: 니트A·앞치마) · 연우(sera_villainess / 의상: 명품홈웨어A)
    무드/조명: 차가운 데이라이트, 블루-그레이 톤. 넓고 화려하지만 냉랭한 공간감

    ─ 클립 2-A · 8초 · 찻잔을 뿜다 ─
    [3초] 미디엄/아이레벨/반측면/연우 위주(서아 걸침) — ...
    에셋: 스틸 (미정) · 영상 (미정) · 레퍼런스 (미정)

씬 헤더 자체(■ 씬N ...)는 pipeline.parsing.split_scenes()가 이미 (num, header, body)로
쪼개주므로, 이 모듈은 그 header와 body를 받아 더 깊이 판다."""
import re

# ── 상태 머신 (문서 90~113행) ────────────────────────────────────────────
SCENE_STATES = [
    "pending", "drafting", "validating", "references_ready",
    "stills_ready", "approved", "video_generating", "completed",
]


def next_state(current: str) -> str:
    """다음 상태로 1단계 전진. 이미 마지막이면 그대로."""
    try:
        idx = SCENE_STATES.index(current)
    except ValueError:
        return SCENE_STATES[0]
    return SCENE_STATES[min(idx + 1, len(SCENE_STATES) - 1)]


def is_completed(state: str) -> bool:
    return state == "completed"


# ── 시간 규칙 (전제 55~57행, 규칙 1 194~204행) ──────────────────────────
CLIP_SECONDS_MIN, CLIP_SECONDS_MAX = 5, 15
EPISODE_SECONDS_MIN, EPISODE_SECONDS_MAX = 90, 110
_TIME_TOLERANCE = 0.5  # 블록 합과 클립 선언 초 사이 허용 오차(반올림 등)

# ── 정규식 ───────────────────────────────────────────────────────────────
SCENE_HEADER_RE = re.compile(
    r"씬\s*(\d+)\s*[·∙•]\s*\[([^\]]+)\]\s*(?:\(([^)]+)\))?\s*[·∙•]\s*"
    r"(?:총\s*)?(\d+(?:\.\d+)?)\s*초\s*[·∙•]\s*(.+)$"
)
CAST_LINE_RE = re.compile(r"(?m)^등장\s*[:：]\s*(.+)$")
CAST_ENTRY_RE = re.compile(r"([^()·]+?)\(([^/()]+)\s*/\s*의상\s*[:：]\s*([^)]+)\)")
MOOD_LINE_RE = re.compile(r"(?m)^무드/조명\s*[:：]\s*(.+)$")
PROP_LINE_RE = re.compile(r"(?m)^소품\s*[:：]\s*(.+)$")
ACTION_LINE_RE = re.compile(r"(?m)^라인\s*[:：]\s*(.+)$")
CLIP_HDR_RE = re.compile(
    r"(?m)^[ \t]*─+\s*클립\s*([0-9]+)-([A-Za-z가-힣])\s*[·∙•]\s*"
    r"(\d+(?:\.\d+)?)\s*초\s*[·∙•]\s*(.+?)\s*─+\s*$"
)
BLOCK_START_RE = re.compile(r"\[(\d+(?:\.\d+)?)\s*초\]")
ASSET_LINE_RE = re.compile(r"에셋\s*[:：]\s*(.+)")


def parse_scene_header(header_line: str) -> dict:
    """'씬N · [장소태그] (element_id) · 총초 · 제목' → dict. 형식이 안 맞으면 필드가 None
    (레거시 콘티 호환 — 파싱 실패를 예외로 던지지 않는다, 상위에서 검증 오류로 취급)."""
    m = SCENE_HEADER_RE.search(header_line or "")
    if not m:
        return {"scene_num": None, "location_tag": None, "location_element_id": None,
                "declared_seconds": None, "title": None}
    return {
        "scene_num": int(m.group(1)),
        "location_tag": m.group(2).strip(),
        "location_element_id": (m.group(3) or "").strip() or None,
        "declared_seconds": float(m.group(4)),
        "title": m.group(5).strip(),
    }


def parse_scene_declarations(scene_body: str) -> dict:
    """씬 공통 선언(등장·무드/조명·소품·라인) — 클립마다 반복하지 않고 씬당 1회 선언되는
    값(0-C 의상 잠금·0-L 장소 레지스트리의 근거). 첫 클립 마커 이전 구간에서만 찾는다."""
    first_clip = CLIP_HDR_RE.search(scene_body)
    prefix = scene_body[:first_clip.start()] if first_clip else scene_body

    cast = []
    cast_m = CAST_LINE_RE.search(prefix)
    if cast_m:
        for name, elem_id, costume in CAST_ENTRY_RE.findall(cast_m.group(1)):
            cast.append({"name": name.strip(), "element_id": elem_id.strip(),
                        "costume": costume.strip()})

    mood_m = MOOD_LINE_RE.search(prefix)
    prop_m = PROP_LINE_RE.search(prefix)
    action_m = ACTION_LINE_RE.search(prefix)
    return {
        "cast": cast,
        "mood": mood_m.group(1).strip() if mood_m else None,
        "props_raw": prop_m.group(1).strip() if prop_m else None,
        "action_line_raw": action_m.group(1).strip() if action_m else None,
    }


def _parse_blocks(chunk: str) -> list[dict]:
    """클립 마커 이후 텍스트에서 [N초] 블록들을 뽑는다. 블록 서술이 여러 줄로 줄바꿈돼 있어도
    (마크다운 워드랩) 다음 [N초] 마커 전까지를 한 블록으로 모아 공백을 정규화한다."""
    marks = list(BLOCK_START_RE.finditer(chunk))
    out = []
    for i, m in enumerate(marks):
        seconds = float(m.group(1))
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(chunk)
        body = chunk[start:end]
        asset_m = ASSET_LINE_RE.search(body)
        if asset_m:
            body = body[:asset_m.start()]
        text = " ".join(body.split()).strip()
        header, _, desc = text.partition("—")
        out.append({
            "seconds": seconds,
            "text": text,
            "header": header.strip() or None,
            "description": desc.strip() or None,
        })
    return out


def parse_clips(scene_body: str) -> list[dict]:
    """씬 콘티 본문 → [{clip_id, declared_seconds, label, blocks, asset_line}, ...].
    클립 마커(─ 클립 N-X · … ─)가 없는 레거시 콘티는 본문 전체를 클립 하나로 취급하고
    declared_seconds는 블록 합으로 대신한다(검증 시 "선언값 없음"으로 표시)."""
    markers = list(CLIP_HDR_RE.finditer(scene_body))
    if not markers:
        blocks = _parse_blocks(scene_body)
        if not blocks:
            return []
        asset_m = ASSET_LINE_RE.search(scene_body)
        return [{
            "clip_id": "1-A", "declared_seconds": None, "label": None, "blocks": blocks,
            "asset_line": asset_m.group(1).strip() if asset_m else None,
        }]

    clips = []
    for i, m in enumerate(markers):
        scene_num, letter = m.group(1), m.group(2)
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(scene_body)
        chunk = scene_body[start:end]
        asset_m = ASSET_LINE_RE.search(chunk)
        clips.append({
            "clip_id": f"{scene_num}-{letter}",
            "declared_seconds": float(m.group(3)),
            "label": m.group(4).strip(),
            "blocks": _parse_blocks(chunk),
            "asset_line": asset_m.group(1).strip() if asset_m else None,
        })
    return clips


def parse_scene(header_line: str, scene_body: str) -> dict:
    """헤더 줄 + 본문 → 정규화된 Scene dict. pipeline.parsing.split_scenes()가 뽑아준
    (num, header, body) 튜플의 header/body를 그대로 넣으면 된다."""
    header = parse_scene_header(header_line)
    decl = parse_scene_declarations(scene_body)
    clips = parse_clips(scene_body)
    total_seconds = round(sum(c["declared_seconds"] or 0 for c in clips), 2)
    return {
        **header,
        **decl,
        "clips": clips,
        "clip_seconds_total": total_seconds,
        "state": "drafting",
    }


# ── 검증 (셀프체크 365~386행 중 결정론적으로 기계 검증 가능한 항목) ──────

def validate_clip(clip: dict) -> list[str]:
    """클립 하나의 시간 규칙 위반을 사람이 읽을 문자열 리스트로. 통과하면 빈 리스트."""
    errors = []
    cid = clip.get("clip_id", "?")
    declared = clip.get("declared_seconds")
    block_sum = round(sum(b["seconds"] for b in clip.get("blocks") or []), 2)
    if declared is None:
        errors.append(f"클립 {cid}: 클립 길이 선언(─ 클립 … ─)이 없어요 — 콘티가 v3.1 클립 "
                      f"마커 형식이 아닐 수 있어요.")
    else:
        if not (CLIP_SECONDS_MIN <= declared <= CLIP_SECONDS_MAX):
            errors.append(f"클립 {cid}: 길이 {declared}초가 {CLIP_SECONDS_MIN}~"
                          f"{CLIP_SECONDS_MAX}초 범위를 벗어났어요.")
        if abs(block_sum - declared) > _TIME_TOLERANCE:
            errors.append(f"클립 {cid}: 블록 합({block_sum}초)이 선언된 클립 길이"
                          f"({declared}초)와 안 맞아요.")
    if not clip.get("blocks"):
        errors.append(f"클립 {cid}: 블록([N초] …)이 하나도 없어요.")
    return errors


def _validate_scene_skeleton_fields(scene: dict) -> list[str]:
    """씬 헤더·선언·클립 시간 배분 검증 — 블록 유무와 무관한 부분(3·5단계 공통)."""
    errors = []
    if scene.get("scene_num") is None:
        errors.append("씬 헤더를 'v3.1 형식(■ 씬N · [장소태그] (element_id) · 총초 · 제목)'으로 "
                      "파싱하지 못했어요.")
    if scene.get("declared_seconds") is not None:
        clip_total = scene.get("clip_seconds_total", 0)
        if abs(clip_total - scene["declared_seconds"]) > _TIME_TOLERANCE:
            errors.append(f"씬 헤더의 총초({scene['declared_seconds']}초)와 클립 길이 합"
                          f"({clip_total}초)이 안 맞아요.")
    if not scene.get("cast"):
        errors.append("등장 라인(등장: 이름(element_id / 의상: …))이 없거나 못 읽었어요.")
    if not scene.get("mood"):
        errors.append("무드/조명 라인이 없어요.")
    if not scene.get("clips"):
        errors.append("클립이 하나도 없어요.")
    return errors


def validate_skeleton_scene(scene: dict) -> list[str]:
    """★3단계(화 전체 1~4단계 뼈대) 전용 검증 — 이 단계는 아직 컷 상세([N초] 블록)를 만들지
    않는 게 정상이므로 블록 존재·블록 합 일치는 검사하지 않는다. 씬 헤더·선언·클립 시간
    범위(5~15초)만 결정론적으로 확인한다(문서의 4단계: 클립 분할 및 러닝타임 검증)."""
    errors = _validate_scene_skeleton_fields(scene)
    for clip in scene.get("clips") or []:
        cid = clip.get("clip_id", "?")
        declared = clip.get("declared_seconds")
        if declared is None:
            errors.append(f"클립 {cid}: 클립 길이 선언(─ 클립 … ─)이 없어요.")
        elif not (CLIP_SECONDS_MIN <= declared <= CLIP_SECONDS_MAX):
            errors.append(f"클립 {cid}: 길이 {declared}초가 {CLIP_SECONDS_MIN}~"
                          f"{CLIP_SECONDS_MAX}초 범위를 벗어났어요.")
    return errors


def validate_scene(scene: dict) -> list[str]:
    """★5단계 이후(씬별 상세 블록 완성) 전용 검증 — 블록 존재·블록 합 일치까지 포함한
    전체 검증. 통과하면 빈 리스트."""
    errors = _validate_scene_skeleton_fields(scene)
    for clip in scene.get("clips") or []:
        errors.extend(validate_clip(clip))
    return errors


def validate_episode_timing(scenes: list[dict]) -> list[str]:
    """화 전체 러닝타임(90~110초) 검증 — 씬별이 아니라 화 전체에서만 판단(문서 20단계)."""
    total = round(sum(s.get("clip_seconds_total") or 0 for s in scenes), 2)
    if not (EPISODE_SECONDS_MIN <= total <= EPISODE_SECONDS_MAX):
        return [f"화 전체 길이 {total}초가 {EPISODE_SECONDS_MIN}~{EPISODE_SECONDS_MAX}초 "
                f"범위를 벗어났어요 — 콘티 블록을 조작하지 말고 대본 분량 문제로 보고하세요."]
    return []


# ── 씬 간 연속성 핸드오프 (문서 206~225행) ────────────────────────────────

def build_scene_handoff(scene: dict, prev_handoff: dict | None = None) -> dict:
    """완료된 씬이 다음 씬에 넘기는 상태. wardrobe/location/lighting은 이 씬의 선언 라인에서
    뽑고, character_state/last_approved_still은 상위(스틸·영상 파이프라인)가 채워 넣을 자리를
    비워둔다(이 모듈은 순수 파싱이라 그 값들의 출처인 생성 결과를 모른다)."""
    wardrobe = {c["name"]: c["costume"] for c in scene.get("cast") or []}
    handoff = {
        "wardrobe": wardrobe,
        "props": (prev_handoff or {}).get("props", {}),
        "location": scene.get("location_tag"),
        "lighting": scene.get("mood"),
        "character_state": {},
        "last_approved_still": None,
    }
    return handoff


# ── 클립 단위 스틸·영상 선택/추출 (문서 스틸/영상 규칙, 규칙 3) ──────────────
# 5단계 산출물(parse_scene의 scene["clips"][].blocks[])을 6~8단계(레퍼런스·스틸·영상)가
# 쓰기 좋게 골라주는 순수 유틸 — API·프롬프트 문자열은 여기 두지 않는다(orchestrator/prompts 몫).

_SUPPLEMENTARY_RE = re.compile(r"보강\s*컷")


def block_pose(block: dict) -> str:
    """블록 서술에서 '자세:'(정지 상태 = 스틸용) 부분만 뽑는다. '/ 동작:'(움직임 = 영상용) 이후는
    버린다. 자세/동작 라벨이 없으면 description(구도 헤더 '—' 뒤) 전체를 그대로 돌려준다."""
    desc = block.get("description") or block.get("text") or ""
    m = re.search(r"자세\s*[:：]\s*(.+)", desc)
    body = m.group(1) if m else desc
    body = re.split(r"/\s*동작\s*[:：]", body)[0]
    return body.strip()


def representative_block(clip: dict) -> dict | None:
    """클립의 대표 스틸용 블록(규칙 3 — 그 클립의 감정·상황을 가장 잘 요약하는 블록). 인서트는
    피하고(디테일 컷이라 대표성이 낮다) 가장 긴 비인서트 블록을 고르되, 동률·전부 인서트면 첫
    블록. 이 선택 하나가 클립당 대표 스틸 1장의 근거가 된다(문서 '스틸' 절)."""
    blocks = clip.get("blocks") or []
    if not blocks:
        return None
    non_insert = [b for b in blocks if "인서트" not in (b.get("header") or "")]
    pool = non_insert or blocks
    return max(pool, key=lambda b: b.get("seconds") or 0)


def supplementary_blocks(clip: dict) -> list[dict]:
    """대표 스틸 하나로 못 받치는 보강컷 블록들(콘티에 '(보강컷 필요)'로 표시된 것) — 문서
    '스틸' 절: 핵심 인서트나 크게 다른 감정 구도만 보강 생성한다."""
    return [b for b in (clip.get("blocks") or []) if _SUPPLEMENTARY_RE.search(b.get("text") or "")]


def scene_prop_names(scene: dict) -> list[str]:
    """씬 소품 선언(소품: 이름A = 스펙 · 이름B = 스펙)에서 소품 이름만. '=' 앞이 이름."""
    raw = scene.get("props_raw")
    if not raw:
        return []
    names = []
    for part in re.split(r"\s*[·∙•]\s*", raw):
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


# ── 소리 층 추출 (규칙 2 소리 층: 립싱크/off/V.O.) — 10단계 오디오 분리용 ──────
# 립싱크·off는 생성 영상 자체 오디오로 커버되고, V.O.(내레이션·속마음)만 별도 나레이션/TTS 층
# 후보다(문서 '소리' 절). 합본이 caption을 통째로 나레이션으로 읽던 것을 이 분리로 대체한다.
_Q = '["“”\'‘’]'  # 큰/작은따옴표(직선·곡선) 문자 클래스
_LIPSYNC_RE = re.compile(
    rf'(\S+?)\s*대사\s*(\(off\)|\(오프\))?\s*[:：]\s*{_Q}(.+?){_Q}\s*(\(립싱크\))?')
_VO_RE = re.compile(rf'(\S+?)\s+V\.?\s*O\.?\s*[:：]\s*{_Q}(.+?){_Q}')


def block_sound_layers(block: dict) -> list[dict]:
    """블록 서술에서 소리 층을 뽑는다 → [{kind: 'lipsync'|'off'|'vo', speaker, text}, ...].
    등장 순서(위치)대로 정렬. 대사(off)=off, 대사(립싱크)/그냥 대사=lipsync, V.O.=vo."""
    text = block.get("text") or ""
    hits = []
    for m in _VO_RE.finditer(text):
        hits.append((m.start(), {"kind": "vo", "speaker": m.group(1).strip(),
                                 "text": m.group(2).strip()}))
    for m in _LIPSYNC_RE.finditer(text):
        # V.O. 매치와 겹치는 위치는 건너뛴다(같은 대사를 이중 집계 방지).
        if any(abs(pos - m.start()) < 3 for pos, _ in hits):
            continue
        kind = "off" if m.group(2) else "lipsync"
        hits.append((m.start(), {"kind": kind, "speaker": m.group(1).strip(),
                                 "text": m.group(3).strip()}))
    return [h for _, h in sorted(hits, key=lambda x: x[0])]


def clip_vo_lines(clip: dict) -> list[dict]:
    """클립의 V.O.(내레이션/속마음) 대사만 블록 순서대로 — 별도 나레이션 TTS 층 후보(10단계)."""
    out = []
    for b in clip.get("blocks") or []:
        out.extend(layer for layer in block_sound_layers(b) if layer["kind"] == "vo")
    return out


def scene_vo_lines(scene: dict) -> list[dict]:
    """씬 전체 V.O. 대사(클립·블록 순서대로)."""
    out = []
    for c in scene.get("clips") or []:
        out.extend(clip_vo_lines(c))
    return out


def scene_element_needs(scene: dict) -> list[tuple[str, str]]:
    """이 씬 하나를 그리는 데 실제로 필요한 (요소명, 타입) 목록 — 6단계(씬별 지연 레퍼런스
    생성)의 입력. 인물(person)은 이미 얼굴 초상 레퍼런스가 있으므로 제외하고, 이 씬 선언에
    등장하는 장소·의상·소품만 모은다(화 전체가 아니라 딱 이 씬 것만 — 지연 생성의 핵심)."""
    needs: list[tuple[str, str]] = []
    seen = set()

    def add(name, etype):
        name = (name or "").strip()
        key = (name, etype)
        if name and key not in seen:
            seen.add(key)
            needs.append(key)

    if scene.get("location_tag"):
        add(scene["location_tag"], "place")
    for c in scene.get("cast") or []:
        add(c.get("costume"), "costume")
    for name in scene_prop_names(scene):
        add(name, "prop")
    return needs
