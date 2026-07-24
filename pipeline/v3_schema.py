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
    [3초] 미디엄(허리 위)/아이레벨/반측면-좌/연우 위주(서아 걸침) — ...
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


# ── 시간 규칙 ──────────────────────────────────────────────────────────────
# ★2026-07-23(컷 단위 전환): 클립층을 제거하고 '컷'을 유일한 제작 단위로 삼는다. 컷 하나 =
# 구도 하나 = 스틸 1장 = 영상 1개. 컷 길이는 단일 구도가 유지되는 짧은 시간(인서트 1~2초,
# 일반 3~5초, 감정 정점 6~8초)이라 1~8초로 잡는다.
CUT_SECONDS_MIN, CUT_SECONDS_MAX = 1, 8
EPISODE_SECONDS_MIN, EPISODE_SECONDS_MAX = 90, 130  # 2026-07-24 상한 120→130(목표 120초에 여유)
_TIME_TOLERANCE = 0.5  # 컷 초 합과 씬 총초 사이 허용 오차(반올림 등)

# ── 정규식 ───────────────────────────────────────────────────────────────
# 구분자 문자 클래스 — LLM이 ·/∙/•/‧/・/|// 등을 섞어 써도 견디게 한다(실 출력 변동성 대응).
_SEP = r"[·∙•‧・|/]"
CAST_LINE_RE = re.compile(r"(?m)^등장\s*[:：]\s*(.+)$")
CAST_ENTRY_RE = re.compile(r"([^()·]+?)\(([^/()]+)\s*/\s*의상\s*[:：]\s*([^)]+)\)")
MOOD_LINE_RE = re.compile(r"(?m)^무드/?조명\s*[:：]\s*(.+)$")
PROP_LINE_RE = re.compile(r"(?m)^소품\s*[:：]\s*(.+)$")
ACTION_LINE_RE = re.compile(r"(?m)^라인\s*[:：]\s*(.+)$")
# 컷 마커: '─ 컷 N-M · N초 · 라벨 ─' (N=씬번호, M=컷번호). 구분자 관대 + 컷번호 앞 -–— + 끝 ─ 선택.
CUT_HDR_RE = re.compile(
    r"(?m)^[ \t]*─+\s*컷\s*([0-9]+)\s*[-–—]\s*([0-9]+)\s*" + _SEP + r"\s*"
    r"(\d+(?:\.\d+)?)\s*초\s*" + _SEP + r"\s*(.+?)\s*(?:─+\s*)?$"
)


def parse_scene_header(header_line: str) -> dict:
    """'씬N · [장소태그] (element_id) · 총초 · 제목' → dict. LLM 출력의 구분자·표기 흔들림에
    견디도록 하나의 엄격한 정규식 대신 필드를 독립적으로 뽑는다(씬번호·초는 검증에 쓰이는 핵심
    필드라 특히 견고하게). 못 찾은 필드는 None(파싱 실패를 예외로 던지지 않고 상위 검증에 맡김)."""
    line = (header_line or "").strip()
    m_num = re.search(r"씬\s*(\d+)", line)
    m_sec = re.search(r"(\d+(?:\.\d+)?)\s*초", line)
    m_loc = re.search(r"\[([^\]]+)\]", line)
    m_id = re.search(r"\(([^)]*)\)", line)

    title = None
    if m_sec:  # 제목 = 초 표기 뒤, 앞쪽 구분자/공백 제거한 나머지
        rest = re.sub(r"^\s*[·∙•‧・|/\-–—]?\s*", "", line[m_sec.end():]).strip()
        title = rest or None

    return {
        "scene_num": int(m_num.group(1)) if m_num else None,
        "location_tag": m_loc.group(1).strip() if m_loc else None,
        "location_element_id": ((m_id.group(1).strip() or None) if m_id else None),
        "declared_seconds": float(m_sec.group(1)) if m_sec else None,
        "title": title,
    }


def parse_scene_declarations(scene_body: str) -> dict:
    """씬 공통 선언(등장·무드/조명·소품·라인) — 컷마다 반복하지 않고 씬당 1회 선언되는
    값(0-C 의상 잠금·0-L 장소 레지스트리의 근거). 첫 컷 마커 이전 구간에서만 찾는다."""
    first_cut = CUT_HDR_RE.search(scene_body)
    prefix = scene_body[:first_cut.start()] if first_cut else scene_body

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


def parse_cuts(scene_body: str) -> list[dict]:
    """씬 콘티 본문 → 컷 리스트. 컷 하나 = 구도 하나 = 스틸 1장 = 영상 1개.
    ★2026-07-23: 클립층 제거. 각 컷 마커(─ 컷 N-M · N초 · 라벨 ─) 아래 서술이 그 컷의 전부다.

    반환 형태는 스틸·영상·참조 생성 기계(produce_clip / _clip_pseudo_shot / clip_motion_prompt
    등)를 손대지 않고 그대로 재사용하려고 '블록 1개짜리 클립' 모양을 유지한다 —
    {clip_id, declared_seconds, label, blocks:[단일 블록]}. blocks의 유일 원소가 곧 그 컷이며,
    seconds는 컷 마커의 선언 초, header/description은 서술을 '—'로 가른 값이다."""
    markers = list(CUT_HDR_RE.finditer(scene_body))
    cuts = []
    for i, m in enumerate(markers):
        scene_num, cut_no = m.group(1), m.group(2)
        seconds = float(m.group(3))
        label = m.group(4).strip()
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(scene_body)
        text = " ".join(scene_body[start:end].split()).strip()
        header, _, desc = text.partition("—")
        block = {
            "seconds": seconds,
            "text": text,
            "header": header.strip() or None,
            "description": desc.strip() or None,
        }
        cuts.append({
            "clip_id": f"{scene_num}-{cut_no}",
            "declared_seconds": seconds,
            "label": label,
            "blocks": [block],
        })
    return cuts


def parse_scene(header_line: str, scene_body: str) -> dict:
    """헤더 줄 + 본문 → 정규화된 Scene dict. pipeline.parsing.split_scenes()가 뽑아준
    (num, header, body) 튜플의 header/body를 그대로 넣으면 된다."""
    header = parse_scene_header(header_line)
    decl = parse_scene_declarations(scene_body)
    cuts = parse_cuts(scene_body)
    total_seconds = round(sum(c["declared_seconds"] or 0 for c in cuts), 2)
    # 컷이 실제 제작 단위(각 컷 → 영상 1개)이므로, 헤더 총초와 컷 합이 다르면 컷 합을 권위값으로
    # 채택한다 — LLM이 헤더 총초를 잘못 더해도(실측) 하드 실패시키지 않는다. 뼈대(3단계)엔 컷이
    # 아직 없어 컷 합=0 → 헤더 총초를 그대로 쓴다(화 전체 러닝타임은 헤더 총초로 검증).
    declared = header.get("declared_seconds")
    if cuts:
        declared = total_seconds
    # 내부 표현은 클립 기계 재사용을 위해 key 'clips'를 유지한다(각 원소가 곧 컷).
    return {
        **header,
        "declared_seconds": declared,
        **decl,
        "clips": cuts,
        "clip_seconds_total": total_seconds,
        "state": "drafting",
    }


# ── 구도 헤더 검증 (프롬프트 "구도 헤더 — 블록 첫머리 4값 고정" 절) ──────────
# 지금까지는 이 4값(샷/앵글/방향/대상)의 형식·어휘 준수를 코드가 전혀 검사하지 않고 LLM
# 프롬프트 지시에만 맡겼다(★2026-07-22) — 여기서부터 결정론적으로 기계 검증한다.

_SHOT_TYPES = ("전신", "미디엄", "클로즈업", "익스트림클로즈업", "인서트")
_SHOT_NEEDS_CUTLINE = ("미디엄", "클로즈업")  # "클로즈업·미디엄은 컷라인 생략 금지"
_ANGLES = ("아이레벨", "로우", "하이")
_DIR_NEEDS_SIDE = ("반측면", "측면")  # "단독·위주 샷은 좌/우 생략 금지(2인 대칭은 생략 가능)"
# 위주 태그 괄호 안은 '{상대} 걸침'이 기본이되, OTS 컷에선 뒤에 '·프레임 좌/우'가 더 붙는다
# (예: '선우 위주(리안 어깨 걸침·프레임 왼쪽)') — 그래서 괄호 안 내용은 열어두고 괄호 유무만 본다.
_TARGET_RE = re.compile(
    r"^(2인|인서트-.+|.+?\s위주\(.+?\)|.+?\s단독)$")


def parse_composition_header(header: str) -> dict:
    """구도 헤더 문자열(예: '클로즈업(가슴 위)/아이레벨/반측면-좌/이수진 위주(강태민 걸침)')을
    {샷, 앵글, 방향, 대상} 4값으로 쪼갠다. 인서트 샷은 방향을 생략할 수 있어(규칙상 허용) 3
    세그먼트만 있어도 방향=None으로 받아들인다. 슬래시 개수가 이 범위를 벗어나면
    segments에 원본 조각을 그대로 담고 나머지는 None — 검증은 별도 함수가 담당(파싱 자체는
    실패시켜 예외를 던지지 않는다)."""
    parts = [p.strip() for p in (header or "").split("/")]
    shot = parts[0] if len(parts) > 0 else None
    if len(parts) == 4:
        angle, direction, target = parts[1], parts[2], parts[3]
    elif len(parts) == 3 and shot and shot.startswith("인서트"):
        angle, direction, target = parts[1], None, parts[2]
    else:
        angle = parts[1] if len(parts) > 1 else None
        direction = parts[2] if len(parts) > 2 else None
        target = parts[3] if len(parts) > 3 else (parts[-1] if len(parts) > 2 else None)
    return {"shot": shot, "angle": angle, "direction": direction, "target": target,
           "segment_count": len(parts)}


def validate_composition_header(header: str | None) -> list[str]:
    """구도 헤더 4값(샷/앵글/방향/대상)의 형식·어휘를 검증. 통과하면 빈 리스트.
    프롬프트(SCENE_BLOCKS_ROLE)가 지시하는 규칙 중 기계적으로 판정 가능한 것만 검사한다 —
    문장 구조 판단(예: "구도가 자연스러운가")까지는 못하고, 정해진 값·표기 형식만 본다."""
    if not header:
        return ["구도 헤더가 없어요(블록이 '샷/앵글/방향/대상 — 서술' 형식이 아니에요)."]
    h = parse_composition_header(header)
    errors = []

    shot = h["shot"]
    if not shot or not shot.startswith(_SHOT_TYPES):
        errors.append(f"구도 헤더 '{header}': 샷 값이 {'/'.join(_SHOT_TYPES)} 중 하나로 "
                      f"시작하지 않아요.")
    elif shot.startswith(_SHOT_NEEDS_CUTLINE) and "(" not in shot:
        errors.append(f"구도 헤더 '{header}': {shot} 샷은 컷라인을 괄호로 명시해야 해요"
                      f"(예: 미디엄(허리 위)).")

    if h["segment_count"] not in (3, 4):
        errors.append(f"구도 헤더 '{header}': 슬래시로 나눈 값이 {h['segment_count']}개예요 — "
                      f"'샷/앵글/방향/대상' 4개(인서트는 방향 생략 시 3개)여야 해요.")

    angle = h["angle"]
    if angle not in _ANGLES:
        errors.append(f"구도 헤더 '{header}': 앵글 값 '{angle}'이 {'/'.join(_ANGLES)} 중 "
                      f"하나가 아니에요.")

    target = h["target"]
    if not target or not _TARGET_RE.match(target):
        errors.append(f"구도 헤더 '{header}': 대상 값 '{target}'이 '2인' / '{{이름}} 단독' / "
                      f"'{{이름}} 위주({{상대}} 걸침)' / '인서트-{{서술}}' 형식이 아니에요"
                      f"(대상 칸에 설명 문장을 그대로 쓰지 마세요).")

    direction = h["direction"]
    is_insert_shot = bool(shot and shot.startswith("인서트"))
    is_pair_target = bool(target and target.startswith("2인"))
    if direction is None:
        if not is_insert_shot:
            errors.append(f"구도 헤더 '{header}': 방향 값이 없어요(인서트 샷만 방향을 생략할 "
                          f"수 있어요).")
    elif direction.startswith("OTS"):
        # 프롬프트 스펙: OTS는 방향칸에 맨값 'OTS'만 쓰고, 걸침 어깨의 프레임 좌/우는 대상 괄호
        # 안에 명시한다(예: 'OTS/선우 위주(리안 어깨 걸침·프레임 왼쪽)'). 그래서 방향칸의 OTS엔
        # 좌/우를 요구하지 않는다(대상 괄호 안의 좌/우 표기까지는 기계로 판정하지 않는다).
        pass
    elif direction.startswith(_DIR_NEEDS_SIDE):
        base = direction.split("-")[0]
        if base not in _DIR_NEEDS_SIDE:
            errors.append(f"구도 헤더 '{header}': 방향 값 '{direction}'을 못 알아봤어요.")
        elif "-" not in direction and not is_pair_target:
            errors.append(f"구도 헤더 '{header}': 단독·위주 샷은 방향의 좌/우를 생략할 수 "
                          f"없어요(현재: '{direction}', 대상: '{target}'). 2인 대칭 샷만 생략 "
                          f"가능해요.")
        elif "-" in direction and direction.split("-", 1)[1] not in ("좌", "우"):
            errors.append(f"구도 헤더 '{header}': 방향 좌/우 표기가 '좌'/'우'가 아니에요"
                          f"(현재: '{direction}').")
    elif direction != "정면":
        errors.append(f"구도 헤더 '{header}': 방향 값 '{direction}'이 정면/반측면-좌·우/"
                      f"측면-좌·우/OTS 중 하나가 아니에요.")

    return errors


# ── 검증 (셀프체크 365~386행 중 결정론적으로 기계 검증 가능한 항목) ──────

def validate_cut(cut: dict) -> list[str]:
    """컷 하나의 시간 규칙 + 구도 헤더 형식 위반을 사람이 읽을 문자열 리스트로. 통과하면 빈 리스트.
    컷은 블록 1개짜리 클립이므로 그 단일 블록(=컷 서술)의 구도 헤더만 검사한다."""
    errors = []
    cid = cut.get("clip_id", "?")
    declared = cut.get("declared_seconds")
    blocks = cut.get("blocks") or []
    if declared is None:
        errors.append(f"컷 {cid}: 길이 선언(─ 컷 … ─)이 없어요 — 콘티가 컷 마커 형식이 아닐 수 있어요.")
    elif not (CUT_SECONDS_MIN <= declared <= CUT_SECONDS_MAX):
        errors.append(f"컷 {cid}: 길이 {declared}초가 {CUT_SECONDS_MIN}~"
                      f"{CUT_SECONDS_MAX}초 범위를 벗어났어요.")
    if not blocks:
        errors.append(f"컷 {cid}: 서술이 비어 있어요.")
    else:
        for msg in validate_composition_header(blocks[0].get("header")):
            errors.append(f"컷 {cid}: {msg}")
    return errors


def _validate_scene_skeleton_fields(scene: dict) -> list[str]:
    """씬 헤더·선언 검증 — 컷 유무와 무관한 부분(3·5단계 공통)."""
    errors = []
    if scene.get("scene_num") is None:
        errors.append("씬 헤더를 'v3.1 형식(■ 씬N · [장소태그] (element_id) · 총초 · 제목)'으로 "
                      "파싱하지 못했어요.")
    # 씬 총초 vs 컷 합 불일치는 parse_scene에서 컷 합으로 자동 보정하므로 여기서 검사 안 함
    # (LLM이 헤더 총초를 잘못 더해도 컷이 제작 단위라 컷 합을 따른다).
    if not scene.get("cast"):
        errors.append("등장 라인(등장: 이름(element_id / 의상: …))이 없거나 못 읽었어요.")
    if not scene.get("mood"):
        errors.append("무드/조명 라인이 없어요.")
    return errors


def validate_skeleton_scene(scene: dict) -> list[str]:
    """★3단계(화 전체 뼈대: 씬 헤더+선언+총초) 전용 검증 — 이 단계는 컷을 아직 만들지 않으므로
    컷 존재·컷 시간은 검사하지 않는다. 씬 헤더·선언만 결정론적으로 확인한다."""
    return _validate_scene_skeleton_fields(scene)


def validate_scene(scene: dict) -> list[str]:
    """★5단계 이후(씬별 컷 완성) 전용 검증 — 컷 존재·컷별 시간/구도까지 포함한 전체 검증.
    통과하면 빈 리스트."""
    errors = _validate_scene_skeleton_fields(scene)
    if not scene.get("clips"):
        errors.append("컷이 하나도 없어요.")
    for cut in scene.get("clips") or []:
        errors.extend(validate_cut(cut))
    return errors


def validate_episode_timing(scenes: list[dict]) -> list[str]:
    """화 전체 러닝타임(90~130초) 검증 — 씬별이 아니라 화 전체에서만 판단. 뼈대(컷 없음)든
    상세콘티(컷 있음)든 씬 declared_seconds(컷 있으면 컷 합, 없으면 헤더 총초) 합으로 본다."""
    total = round(sum(s.get("declared_seconds") or 0 for s in scenes), 2)
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
    """컷의 스틸용 블록. ★2026-07-23 컷 단위 전환 이후 컷은 블록 1개짜리라 그 단일 블록을 그대로
    돌려준다(예전 '클립 안 여러 블록 중 대표 선택'은 컷 하나=구도 하나가 되며 무의미). 방어적으로
    블록이 여럿이면 인서트를 피해 가장 긴 것을 고른다(구형/손수정 콘티 대비)."""
    blocks = clip.get("blocks") or []
    if not blocks:
        return None
    if len(blocks) == 1:
        return blocks[0]
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
        costume = c.get("costume")
        if costume and "미등록" not in costume:  # '⚠ 미등록'은 실제 의상 아님 → 요소로 만들지 않음
            add(costume, "costume")
    for name in scene_prop_names(scene):
        add(name, "prop")
    return needs
