# -*- coding: utf-8 -*-
"""한 줄 아이디어 → 기획안 → 대본 → 씬설계 → 상세콘티 → 샷분해 (1~5단계, 텍스트만).
이미지·영상·합본(6~8단계)은 나중에 이어붙인다. 모든 LLM/HTTP 호출은 vendor의 기존 함수 재사용."""
import concurrent.futures
import hashlib
import io
import os
import re
import threading

import vendor.cowriter.bot.prompts as cw_prompts
import vendor.storyboard.bot.config as sb_config
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


def _cw_complete(system: str, user: str) -> str:
    """코라이터 텍스트 완성 — 모든 기획·대본 텍스트는 OpenRouter만 사용한다."""
    return oi.chat(system, user)


CHAT_SYSTEM = """너는 숏폼 드라마 기획을 같이 구상하는 친근하고 리액션 좋은 파트너다.
지금은 로맨스만 프로덕션 가능(레퍼런스DB·템플릿이 로맨스 전용) — 나중에 다른 장르가 추가될
수도 있지만, 지금은 항상 로맨스를 뼈대로 두고 그 안에서 좁혀라. "로맨스냐 스릴러냐"처럼 로맨스
자체를 벗어나는 선택지는 주지 말고, 스릴러/서스펜스 같은 느낌은 로맨스 안의 텐션 정도로만 다뤄라.

사용자의 아이디어나 답변에 짧고 긍정적으로 반응해라("오 좋은데요!" 같은 톤) — 그 소재가 어떤
장르·트렌드에 해당하는지 아는 티를 내면 더 좋다(예: "재벌x알바생 조합, 로맨스 클리셰 중에서도
스테디셀러죠").

매 턴마다 아래 순서로 아직 안 정해진 축을 딱 1개만 좁혀라(이미 아이디어/답변에서 드러난 축은
다시 묻지 말고 그거라고 짚어주기만 하고 다음 축으로 넘어가라):
1. 서브장르/설정(오피스·사극·학원물·재벌물·아이돌물 등) — 이미 명확하면 스킵. 애매하면 구체적인
   선택지 2~4개를 추천해라.
2. 핵심 갈등/훅 — 뭐가 두 사람을 못 만나게 막는지 — 선택지로 추천해라.
3. 텐션/톤 — 코미디/절절함/다크, 서스펜스·스릴러 느낌을 얼마나 섞을지 — 선택지로 추천해라.
   어느 쪽이든 결말은 로맨스로 수렴한다.
4. 엔딩 방향 — 해피/새드/열린 — 선택지로 추천해라.

2~3문장 이내로 짧게, 채팅처럼 답하라 — 마크다운(별표·목록 기호) 쓰지 말고 평문으로만.
기획안·대본 형식(제목·항목)으로 쓰지 마라.

★선택지를 추천할 때마다 반드시 맨 끝에 이 형식의 줄을 한 줄 추가해라(선택지가 없는 열린
질문일 때는 이 줄 자체를 넣지 마라):
[선택지: 짧은라벨1 | 짧은라벨2 | 짧은라벨3]
각 라벨은 2~6글자 내외로 짧게(예: "오피스 로맨스", "사극", "삼각관계"), 앞의 본문 설명과
겹쳐도 된다 — 사용자가 탭 한 번으로 고를 수 있는 용도다.

3~4번 정도 대화가 오갔으면(위 4가지가 대부분 정해졌으면), 이제 구체적인 기획안을 써볼 만하다고
자연스럽게 제안해라(이때는 [선택지] 줄 대신 "기획 시작하기 버튼을 눌러주세요" 같은 안내만)."""

_OPTIONS_LINE_RE = re.compile(r"\n?\[선택지:\s*(.+?)\]\s*$")


def chat_reply(history: list[dict]) -> tuple[str, list[str]]:
    """history: [{"role": "user"|"assistant", "content": str}, ...], 마지막이 사용자 메시지.
    반환: (화면에 보여줄 텍스트, 선택지 칩 라벨 목록 — 없으면 빈 리스트)."""
    lines = [f"[{'사용자' if m['role'] == 'user' else '너'}] {m['content']}" for m in history]
    convo = "\n".join(lines)
    raw = _with_retry(_cw_complete, CHAT_SYSTEM, convo).strip()
    m = _OPTIONS_LINE_RE.search(raw)
    if not m:
        return raw, []
    options = [o.strip() for o in m.group(1).split("|") if o.strip()]
    text = raw[:m.start()].strip()
    return text, options


def compose_idea_from_chat(history: list[dict]) -> str:
    """채팅으로 주고받은 내용을 한 덩어리 컨셉 텍스트로 합쳐 기존 파이프라인의 idea로 사용."""
    lines = [f"{'사용자' if m['role'] == 'user' else '보조'}: {m['content']}" for m in history]
    return "다음은 기획 방향을 잡기 위해 나눈 대화다. 이 내용을 종합해 기획안을 써라.\n\n" + "\n".join(lines)


def generate_pitch(idea: str) -> str:
    user_msg = f"이 컨셉으로 기획안 초안을 만들어줘:\n{idea}"
    return _with_retry(_cw_complete, cw_prompts.plan_system(idea), user_msg).strip()


LOGLINE_SYSTEM = """너는 숏폼 로맨스 드라마 로그라인만 짧게 뽑아주는 카피라이터다.
주어진 내용을 바탕으로 후킹되는 로그라인을 **한두 문장**으로만 써라. 제목·해시태그·설명·
목록은 쓰지 말고, 로그라인 문장 그 자체만 출력해라."""


def generate_logline(idea: str) -> str:
    """전체 기획안(등장인물·줄거리·회차분배 등) 대신 로그라인 한두 문장만 먼저 보여줄 때 사용."""
    user_msg = f"다음 내용을 바탕으로 로그라인만 써줘:\n{idea}"
    return _with_retry(_cw_complete, LOGLINE_SYSTEM, user_msg).strip()


PITCH_CARD_SYSTEM = """너는 숏폼 로맨스 드라마 카피라이터다. 주어진 내용을 바탕으로 로그라인,
두 주인공(남녀 주인공, 정확히 2명), 그리고 1화에 나올 법한 임팩트 있는 한 장면을 JSON 객체
하나로만 출력해라 — 설명·코드펜스 없이 JSON만.

형식:
{"logline": "한두 문장 로그라인",
 "characters": [
   {"name": "이름", "role": "성별/신분 등 짧은 설정(예: '여 · 재벌가 2세')", "line": "그 인물의 핵심 대사 한 줄"},
   {"name": "이름", "role": "...", "line": "..."}
 ],
 "key_scene": {
   "situation": "그 장면 상황을 영어로 1~2문장 — 이미지 생성 프롬프트에 그대로 쓰이니 등장인물
     구도(예: two-shot, medium wide)·장소·분위기·표정/동작을 구체적으로",
   "lines": ["대사 또는 지문 1", "대사 또는 지문 2", "대사 또는 지문 3", "(선택) 대사 4"]
 }}

key_scene은 1화 안에서 가장 훅이 되는 순간(정체 발각 직전, 첫 만남의 결정적 장면 등) 하나를
골라라. lines는 실제 대본처럼 3~4줄, 인물 이름을 대사 앞에 붙여라(예: "유준: 대사")."""


def generate_pitch_card(idea: str) -> dict:
    """로그라인 + 두 주인공(이름/역할/핵심대사) 카드. 실패하면(파싱 오류 포함) 1회 재시도."""
    def _once():
        user_msg = f"다음 내용을 바탕으로 로그라인+두 주인공 카드를 만들어줘:\n{idea}"
        raw = _cw_complete(PITCH_CARD_SYSTEM, user_msg).strip()
        return parsing.parse_json_object(raw)
    return _with_retry(_once)


# ★2026-07-21(실측 — 사용자가 의견창에 의미 없는 텍스트("ㄴㅇㄹㅁㄴㅇㄹ" 등)를 넣으면 AI가
# 실제 산출물 대신 "확인이 필요합니다" 식으로 되묻는 응답을 하는데, 그 응답이 그대로 기존
# 시놉시스/요약/대본을 덮어써버림): 산출물이 아니라 되묻는 응답으로 보이면 저장 전에 걸러낸다.
_CLARIFICATION_MARKERS = (
    "확인이 필요합니다", "명확하지 않습니다", "말씀해 주시", "말씀해주시",
    "구체적으로 알려주시", "다음 중 하나라면", "어떤 것을 원하시", "요청이 누락된",
)


def looks_like_clarification(text: str) -> bool:
    """생성 결과가 실제 산출물이 아니라 AI가 되묻는 응답인지 휴리스틱으로 판별."""
    return any(marker in text for marker in _CLARIFICATION_MARKERS)


SYNOPSIS_SYSTEM = """너는 숏폼 로맨스 드라마 작가다. 주어진 로그라인·등장인물을 바탕으로
전체 줄거리(시놉시스)를 3~5문단으로 써라 — 처음(발단)부터 끝(결말)까지 이야기 전체 흐름을
요약한다: 어떻게 만나는지, 중간에 어떤 갈등·반전이 있는지, 위기가 어떻게 최고조에 달하는지,
결말이 어떻게 나는지까지. 등장인물 이름은 주어진 그대로 써라. 회차 구성표나 항목 목록이
아니라 이야기를 서술하는 문단 형태로, 제목·헤더 없이 본문만 출력해라."""


def generate_synopsis(idea: str, logline: str, characters: list[dict], note: str = "") -> str:
    """스튜디오의 "전체 줄거리" — 개별 화 대본과 달리 작품 전체를 관통하는 이야기 흐름.
    이후 화별 대본 생성 시 바이블처럼 참고할 수 있는 기준점이 된다.
    note: 사용자가 생성 시점에 남긴 의견/요청(선택) — 그대로 프롬프트에 반영한다."""
    names = ", ".join(f"{c.get('name')}({c.get('role')})" for c in characters)
    user_msg = f"아이디어: {idea}\n로그라인: {logline}\n등장인물: {names}\n\n전체 줄거리를 써줘."
    if note.strip():
        user_msg += f"\n\n[작가의 추가 요청 — 반드시 반영]\n{note.strip()}"
    return _with_retry(_cw_complete, SYNOPSIS_SYSTEM, user_msg).strip()


EPISODE_SUMMARY_SYSTEM = """너는 숏폼 로맨스 드라마 편집자다. 주어진 한 화 분량의 대본을 읽고
그 화의 줄거리 요약을 2~3문장으로 써라 — 이 화 안에서 어떤 사건이 벌어지고 어떻게 끝나는지
(엔딩 훅 포함)만 짧게. 대본에 없는 내용을 지어내지 마라. 제목·헤더 없이 본문만 출력해라."""


def generate_episode_summary(script: str, note: str = "") -> str:
    """대본이 있을 때 그 대본을 2~3문장으로 요약(대본 기반).
    note: 사용자가 생성 시점에 남긴 의견/요청(선택) — 그대로 프롬프트에 반영한다."""
    user_msg = f"다음 대본의 줄거리를 2~3문장으로 요약해줘:\n{script}"
    if note.strip():
        user_msg += f"\n\n[작가의 추가 요청 — 반드시 반영]\n{note.strip()}"
    return _with_retry(_cw_complete, EPISODE_SUMMARY_SYSTEM, user_msg).strip()


EPISODE_PLAN_SUMMARY_SYSTEM = """너는 숏폼 로맨스 드라마 작가다. 작품 전체 줄거리(시놉시스)를 바탕으로
'이번 화에서 벌어질 사건'을 2~3문장으로 제안해라 — 이 화가 전체 이야기 중 어느 지점인지 고려해
(1화면 발단·첫 만남, 뒤로 갈수록 갈등·전개·위기·결말) 이 화 안에서 무슨 일이 일어나고 어떻게
끝나는지(엔딩 훅 포함) 쓴다. 이 요약이 이후 대본의 뼈대가 된다. 등장인물 이름은 주어진 그대로
써라. 제목·헤더 없이 본문만 출력해라."""


def generate_episode_plan_summary(num: int, logline: str, synopsis: str,
                                  characters: list[dict] | None = None, note: str = "") -> str:
    """대본이 아직 없을 때 — 전체 줄거리에서 '이번 화 사건'을 뽑아 요약을 먼저 만든다.
    이후 generate_script가 이 요약을 뼈대로 대본을 쓴다(요약 먼저, 대본 나중 흐름).
    note: 사용자가 생성 시점에 남긴 의견/요청(선택) — 그대로 프롬프트에 반영한다."""
    names = ", ".join(f"{c.get('name')}({c.get('role', '')})"
                      for c in (characters or []) if c.get("name"))
    user_msg = (f"[작품 로그라인]\n{logline}\n\n[전체 줄거리]\n{synopsis or '(아직 없음)'}\n\n"
                f"[등장인물]\n{names or '(미정)'}\n\n위를 바탕으로 {num}화에서 벌어질 사건을 "
                f"2~3문장 요약으로 써줘.")
    if note.strip():
        user_msg += f"\n\n[작가의 추가 요청 — 반드시 반영]\n{note.strip()}"
    return _with_retry(_cw_complete, EPISODE_PLAN_SUMMARY_SYSTEM, user_msg).strip()


# storyboard-bot/app.py의 STILL_STYLE·_IDEALIZED_FACE_GUIDANCE(원본은 55~60% 리얼리즘 지정)를
# 가져오되, 사용자 요청으로 리얼리즘 비율만 80%로 올림 — 그 외 "실존 인물처럼 안 보이게",
# "사진이 아니라 스타일화된 일러스트" 지침은 그대로 유지.
PORTRAIT_STYLE = (
    "semi-realistic illustration style, painterly rendering, cinematic still, "
    "natural relaxed facial expression, not stiff or uncanny, "
    "clearly a stylized illustration, not a pure photograph, "
    "not resembling any real celebrity or public figure. "
    "Render at roughly 80% realism — mostly photoreal in lighting and proportion, but keep a "
    "subtle painterly/illustrated quality with softened skin texture (not sharp photographic "
    "pores/texture). "
    "No text, letters, captions, subtitles, or written words anywhere in the image."
)

# 인물 고정 이미지는 '얼굴 전용 레퍼런스'로 만든다 — 이후 샷 생성이 이 이미지를 얼굴 정체성
# 참조(input_references, role=person)로 쓰는데, 전신·강한 의상·액세서리가 들어가면 그 옷/소품까지
# 컷에 딸려 나와 의상 일관성이 깨진다. 그래서 얼굴~어깨 크롭 + 중립 무지 상의 + 액세서리 제거로
# 고정해, 오직 얼굴·헤어 정체성만 담기게 한다.
FACE_REF_FRAMING = (
    "Head-and-shoulders identity headshot, tightly framed on the face (top of head to shoulders "
    "only, no full body). Plain neutral crew-neck t-shirt in a muted solid color, high neckline. "
    "No accessories, no jewelry, no earrings, no glasses, no hats, no scarves. Front-facing or "
    "slight three-quarter view, neutral even studio lighting, plain seamless background. "
    "This is a face reference — do NOT show any distinctive costume, uniform, or outfit."
)


_GENDER_EN = {"남": "male", "여": "female", "male": "male", "female": "female"}


def _face_appearance(appearance: str) -> str:
    """외형 설명에서 얼굴/헤어 정체성만 남기고 옷차림 관련 문구는 뺀다(얼굴 전용 레퍼런스라
    의상 묘사가 크롭에 끼어들면 안 됨). 콤마/문장 단위로 쪼개 의상 키워드가 든 조각을 버린다."""
    if not appearance:
        return ""
    clothing_kw = ("옷", "의상", "입", "착용", "슈트", "정장", "셔츠", "니트", "코트", "재킷", "자켓",
                   "티셔츠", "티", "바지", "치마", "스커트", "드레스", "후드", "패딩", "유니폼",
                   "복장", "차림", "액세서리", "귀걸이", "목걸이", "반지", "시계", "안경", "모자",
                   "wear", "outfit", "suit", "shirt", "dress", "jacket", "coat", "uniform")
    parts = re.split(r"[,\n·]", appearance)
    kept = [p.strip() for p in parts if p.strip() and not any(k in p for k in clothing_kw)]
    return ", ".join(kept)


def generate_character_portrait(character: dict) -> bytes:
    """인물 '얼굴 전용 고정 레퍼런스' 이미지 1장(PNG bytes). 이름·성별·나이·(옷차림을 뺀)외형을
    한 프롬프트로 묶어 얼굴/헤어 정체성만 담고, 얼굴~어깨 크롭·중립 무지 상의·액세서리 없음으로
    고정한다 — 이 이미지가 이후 샷 생성의 얼굴 참조로 쓰이므로 의상·소품이 들어가면 안 된다.
    스틸컷 API라 영상화 때와 달리 클로즈업 안전필터 리스크는 없음(그 필터는 image-to-video 전용)."""
    gender_en = _GENDER_EN.get((character.get("gender") or "").strip(), "")
    age = (character.get("age") or "").strip()
    face_appearance = _face_appearance((character.get("appearance") or "").strip())
    basics = ", ".join(filter(None, [
        gender_en,
        f"{age} years old" if age else "",
    ]))
    appearance_clause = (
        f" Face and hair identity (follow closely): {face_appearance}." if face_appearance else ""
    )
    prompt = (
        f"Character face reference. Subject: {character.get('name', '')} — {basics}.{appearance_clause} "
        f"{FACE_REF_FRAMING} {PORTRAIT_STYLE}"
    )
    png, _cost = _with_retry(oi.generate, prompt, aspect_ratio="2:3", refs=[])
    return png


def _characters_bible_for_tone(characters: list[dict] | None) -> dict:
    """char_add_system이 참고할 "기존 등장인물" 블록용 — 톤·관계 참고 목적이라 이름+주요 필드만."""
    chars = {}
    for c in characters or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        chars[name] = {
            "성별": c.get("gender", ""), "나이": c.get("age", ""),
            "포지션": c.get("role", ""), "외형": c.get("appearance", ""),
            "설정": c.get("description", ""), "핵심대사": c.get("line", ""),
        }
    return chars


_CARD_FIELD_LABELS = {
    "gender": "성별", "age": "나이", "role": "포지션",
    "line": "핵심대사", "appearance": "외형", "description": "설정·설명",
}


def generate_character_card(name: str, hint: str = "", logline: str = "",
                            characters: list[dict] | None = None,
                            existing: dict | None = None) -> dict:
    """이름(+선택 힌트)으로 캐릭터 카드를 AI 생성 — 원본 봇의 char_add 프롬프트 재사용.
    existing에 사용자가 이미 채운 값이 있으면 그 값은 **그대로 유지**하고 비어 있는 칸만 AI가
    채운다(사용자 요청). 로그라인·기존 인물을 참고로 작품 톤/관계에 맞춘다. 반환은 우리 스키마
    (gender/age/role/line/appearance/description)."""
    existing = {k: (v or "").strip() for k, v in (existing or {}).items()}
    filled = {k: v for k, v in existing.items() if v}

    bible = {}
    if logline:
        bible["logline"] = logline
    tone_chars = _characters_bible_for_tone(characters)
    if tone_chars:
        bible["characters"] = tone_chars
    system = cw_prompts.char_add_system(bible or None)

    # 사용자가 이미 채운 칸을 "확정 정보"로 프롬프트에 못박아, AI가 그와 모순되지 않게 나머지를 채우게 한다.
    req_parts = [hint] if hint else []
    if filled:
        locked = "\n".join(f"- {_CARD_FIELD_LABELS.get(k, k)}: {v}" for k, v in filled.items())
        req_parts.append(
            "아래는 작가가 이미 확정한 정보다 — 이 값들과 모순되지 않게, 이 설정에 맞춰 "
            f"나머지 항목을 채워라(확정 항목은 바꾸지 마라):\n{locked}")
    request_text = "\n".join(req_parts) or "이 작품 톤에 어울리는 인물로 자유롭게 만들어줘"
    user = cw_prompts.char_add_user(name or "새 인물", request_text)

    def _once():
        raw = _cw_complete(system, user)
        return parsing.parse_json_object(raw)  # 실패 시 ValueError → _with_retry가 한 번 더

    data = _with_retry(_once)
    desc = " ".join(x for x in [data.get("설정", ""), data.get("설명", "")] if x).strip()
    ai = {
        "gender": (data.get("성별") or "").strip(),
        "age": str(data.get("나이") or "").strip(),
        "role": (data.get("포지션") or "").strip(),
        "line": (data.get("핵심대사") or "").strip(),
        "appearance": (data.get("외형") or "").strip(),
        "description": desc,
    }
    # 사용자가 채운 칸은 그대로 두고, 빈 칸만 AI 값으로 채운다.
    return {k: (existing.get(k) or ai.get(k, "")) for k in ai}


def generate_key_scene_image(situation: str, character_images: list[str] | None = None) -> bytes:
    """기획 카드의 "1화 임팩트 장면" 미리보기 이미지 1장(PNG bytes). character_images(인물 초상화
    data URL 목록)를 참조로 넘기면 그 얼굴이 장면에도 그대로 반영된다 — 안 넘기면 매번 다른
    얼굴이 나옴(사용자 리포트: "임팩트 장면에 인물이 전혀 반영 안 됨")."""
    refs = [img for img in (character_images or []) if img and img.startswith("data:image")]
    prompt = f"Semi-realistic cinematic still, vertical 9:16 framing. Scene: {situation} {PORTRAIT_STYLE}"
    png, _cost = _with_retry(oi.generate, prompt, aspect_ratio="9:16", refs=refs)
    return png


def characters_bible(characters: list[dict] | None) -> dict | None:
    """캐릭터 카드(성별·나이·포지션·외형·설정·핵심대사)를 storyboard_bible_block이 기대하는
    바이블 스키마로 바꾼다 — 원본 봇이 이미 갖고 있던 메커니즘 재사용: 이 바이블을 씬설계·콘티
    시스템 프롬프트에 넣어주면 "외형/의상 일관성을 이 설정에 맞춰라"라는 지시가 같이 붙어서,
    인물 외형이 대본 이후 모든 텍스트 단계(씬설계→콘티→샷)에서 계속 반복 언급된다. 이미지 자체는
    참조 사진(얼굴)으로 유지되지만, 참조가 없거나 참조가 못 주는 디테일(체형·눈매·즐겨입는 옷
    스타일 등)은 이 텍스트 반복이 대신 채운다."""
    if not characters:
        return None
    chars = {}
    for c in characters:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        chars[name] = {
            "성별": c.get("gender", ""),
            "나이": c.get("age", ""),
            "포지션": c.get("role", ""),
            "외형": c.get("appearance", ""),
            "설정": c.get("description", ""),
            "핵심대사": c.get("line", ""),
        }
    return {"characters": chars} if chars else None


def studio_script_bible(project: dict, target_episode: int) -> dict:
    """스튜디오 프로젝트를 cowriter의 전체 작품 바이블 스키마로 변환한다.

    현재 화 요약은 준수할 개요로, 이전 화 대본은 연속성 참고로 넣는다. 이전 대본 전체를
    프롬프트에 반복하지 않고 각 화 요약과 직전 화 엔딩 600자만 쓰는 기존 build_bible_block의
    압축 규칙을 그대로 활용한다.
    """
    char_block = characters_bible(project.get("characters")) or {}
    outlines: dict[str, str] = {}
    episode_plan: dict[str, dict] = {}
    scripts: dict[str, str] = {}
    script_summaries: dict[str, dict] = {}

    for ep in project.get("episodes") or []:
        num = ep.get("num")
        if not isinstance(num, int):
            continue
        key = f"{num}화"
        summary = (ep.get("summary") or "").strip()
        if summary:
            outlines[key] = summary
            episode_plan[key] = {
                "구간": "화별 구성",
                "화수": key,
                "핵심사건": summary,
            }

        # 현재 화의 기존 대본은 재생성 프롬프트에 섞지 않는다. 오직 이전 화만 연속성 자료다.
        script = (ep.get("script") or "").strip()
        if num < target_episode and script:
            scripts[key] = script
            if summary:
                script_summaries[key] = {
                    "summary": summary,
                    "hash": hashlib.md5(script.encode("utf-8")).hexdigest(),
                }

    return {
        "title": project.get("title") or "제목 미정",
        "status_raw": f"{target_episode}화 작업 중",
        "current_episode": target_episode,
        "logline": project.get("logline") or project.get("idea") or "",
        "keyword": project.get("idea") or "",
        "plot": project.get("synopsis") or "",
        "characters": char_block.get("characters", {}),
        "episode_plan": episode_plan,
        "outlines": outlines,
        "scripts": scripts,
        "script_summaries": script_summaries,
    }


def generate_script(idea: str, pitch: str, episode: int = 1,
                    characters: list[dict] | None = None, summary: str = "", note: str = "",
                    bible: dict | None = None) -> str:
    """전체 작품 바이블을 기준으로 한 화 대본을 생성한다.

    bible에는 전체 줄거리·모든 캐릭터·화별 개요·이전 대본 요약·직전 엔딩이 들어간다.
    characters는 그중 이번 화에 실제 등장할 인물을 제한하는 목록이다.
    """
    char_lines = ""
    if characters:
        names = "\n".join(f"- {c.get('name')}({c.get('role', '')})"
                          for c in characters if c.get("name"))
        if names:
            char_lines = ("\n\n[이번 화 등장인물 — 아래 인물만 등장시키고 이름·설정은 작품 바이블을 "
                          f"그대로 따라라]\n{names}")
    summary_lines = ""
    if summary and summary.strip():
        summary_lines = (f"\n\n[이번 화 요약 — 이 사건 흐름·엔딩을 그대로 대본으로 풀어써라, "
                         f"여기서 벗어나지 마라]\n{summary.strip()}")
    note_lines = f"\n\n[작가의 추가 요청 — 반드시 반영]\n{note.strip()}" if note.strip() else ""
    user_msg = f"{pitch}{char_lines}{summary_lines}{note_lines}\n\n위를 바탕으로 {episode}화 대본을 써줘."
    if bible is None:
        # CLI의 단독 파이프라인도 바이블 경로를 타게 한다. 스튜디오에서는 studio_script_bible의
        # 완전한 프로젝트 바이블이 전달된다.
        bible = {
            "title": "제목 미정",
            "current_episode": episode,
            "logline": pitch,
            "keyword": idea,
            "plot": pitch,
            "characters": (characters_bible(characters) or {}).get("characters", {}),
            "outlines": {f"{episode}화": summary} if summary.strip() else {},
        }
    # ROLE+SCRIPT_SPEC+패턴+템플릿+유사사례+전체 작품 바이블을 조립해 OpenRouter로 보낸다.
    blocks = cw_prompts.system_blocks(idea, bible=bible, target_episode=episode, kind="대본")
    system_text = "\n\n".join(b["text"] for b in blocks)
    return _with_retry(_cw_complete, system_text, user_msg).strip()


# 씬설계·콘티 텍스트 생성 백엔드 스위치.
#  - 기본(agent): 로컬 claude CLI — 무료지만 호출당 느림(씬설계 오래 걸리는 원인)
#  - SB_TEXT_BACKEND=openrouter: 이미 이미지·샷분해에 쓰는 OpenRouter(oi.chat)로 같은 프롬프트를
#    보낸다. OPENROUTER_API_KEY 하나로 전 단계가 처리돼 별도 Anthropic 키가 필요 없고 훨씬 빠르다.
_TEXT_BACKEND = os.environ.get("SB_TEXT_BACKEND", "").strip().lower()


def _sb_complete(system: str, user: str) -> str:
    """씬설계·콘티용 텍스트 완성 — 백엔드 스위치에 따라 OpenRouter(oi.chat) 또는 agent(sb_generator)."""
    if _TEXT_BACKEND == "openrouter":
        return oi.chat(system, user)
    return sb_generator.complete(system, user)


def generate_scene_plan(script: str, episode: int = 1,
                        characters: list[dict] | None = None) -> str:
    return _with_retry(
        _sb_complete,
        sb_prompts.storyboard_plan_system(bible=characters_bible(characters), target_episode=episode),
        sb_prompts.storyboard_plan_user(script)).strip()


def generate_conti(script: str, plan_text: str, scenes_plan: list[tuple[int, str]],
                   episode: int = 1, characters: list[dict] | None = None) -> str:
    """상세 콘티. app.py의 씬 단위 병렬 호출(_gen_scene)과 같은 프롬프트 패턴이지만
    MVP는 순차 for-loop로(디버깅 쉬움, 데모 안정성 우선)."""
    sys_prompt = sb_prompts.storyboard_system(bible=characters_bible(characters), target_episode=episode)
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
        parts.append(_with_retry(_sb_complete, sys_prompt, user).strip())
    return "\n\n".join(parts)


ELEMENT_EXTRACT_TYPES = {"place": "places", "prop": "props", "costume": "costumes"}


def extract_and_register_elements(work: str, conti_full: str) -> dict:
    """상세 콘티에서 인물·장소·의상·소품을 뽑아 요소 레지스트리에 자동 등록한다(사진 없이 이름만 —
    등록해두면 샷 분해 단계가 같은 이름을 그대로 재사용하도록 유도돼 컷마다 표현이 흔들리지 않는다).
    사용자에게는 별도 등록 UI 없이 완전히 자동/비가시적으로 진행된다."""
    existing = oi.load_elements(work)
    existing_categories = sorted({
        nm.split("-", 1)[0] for e in existing if e.get("type") == "place"
        for nm in [e.get("display", "")] if "-" in nm
    })
    raw = _with_retry(sb_generator.complete,
                      sb_prompts.element_extract_system(existing_categories or None),
                      sb_prompts.element_extract_user(conti_full))
    data = parsing.parse_json_object(raw)
    for etype, key in ELEMENT_EXTRACT_TYPES.items():
        for name in data.get(key) or []:
            if name:
                oi.register_element(work, name, etype=etype)
    return data


# ── 요소(장소·의상·소품) 고정 레퍼런스 이미지 생성 파이프라인 ──────────────
# co-writer-bot(dispatch_storyboard._generate_element_candidate/_auto_register_element)에서 가져온
# 구조 — 인물 얼굴처럼 "장소/의상/소품"도 레퍼런스 이미지를 만들어 요소 id로 등록해두면, 샷 생성
# 시 shot_refs가 그 이미지를 자동으로 물어(역할별 지시 포함) 컷마다 같은 배경·같은 옷이 나온다.
# 스틸컷과 화풍을 맞추려고 PORTRAIT_STYLE(세미리얼 일러스트)과 같은 스타일 문구를 공유한다.
ELEMENT_REF_STYLE = (
    "Semi-realistic illustration style, painterly cinematic rendering — clearly a stylized "
    "illustration, not a photograph. No text, letters, captions, or watermark anywhere."
)


def _element_ref_prompt(name: str, etype: str, mood: str = "", context: str = "") -> str:
    """요소 타입별 레퍼런스 이미지 프롬프트. mood=작품 톤/장르(로그라인 등), context=콘티 발췌."""
    mood_s = mood.strip()
    ctx = f" Context: {context.strip()[:300]}." if context.strip() else ""
    if etype == "place":
        mood_instr = (
            f"This location is for a short-form drama with this genre/setting/tone: {mood_s}. "
            "Match its design, lighting, color palette, and atmosphere to that context — cinematic "
            "and moody, not a flat neutral stock photo. " if mood_s else
            "Give the location a clear cinematic mood (deliberate lighting, color, shadow). ")
        # 조명·분위기는 영화적으로, 공간 구조·건축 자체는 그 장소 종류의 평범한 모습 유지.
        ordinary = ("The architecture, layout, and furnishings themselves should be ordinary and "
                    "realistic for this type of location — not exotic or surreal. " if not context.strip() else "")
        return (f"{ELEMENT_REF_STYLE} — cinematic empty establishing shot of the location '{name}'. "
                f"{mood_instr}{ordinary}{ctx} No people visible, a clean reference plate.")
    if etype == "costume":
        mood_instr = (
            f"This costume is for a story with this genre/setting/tone: {mood_s}. Make its era, "
            "silhouette, fabric, and styling consistent with that context. " if mood_s else "")
        # 구체 묘사(context)가 없으면 튀지 않는 무난한 기본 디자인으로.
        plain = ("No specific description was given, so default to a plain, understated, minimal "
                 "design — no bold colors, loud patterns, logos, or eye-catching details. "
                 if not context.strip() else "")
        return (f"{ELEMENT_REF_STYLE} reference of a clothing outfit called '{name}', shown as a "
                "flat lay or on an invisible mannequin (no visible face or head), on a plain solid "
                f"white background, studio product-shot lighting. {plain}{mood_instr}{ctx}")
    # prop
    mood_instr = (f"Style/lighting should feel consistent with this drama's genre/setting/tone: "
                  f"{mood_s}. " if mood_s else "")
    return (f"{ELEMENT_REF_STYLE} reference of the object '{name}' alone on a plain neutral "
            f"background. {mood_instr}{ctx}")


def _register_element_image(work: str, name: str, etype: str, png: bytes) -> None:
    """생성한 요소 레퍼런스 PNG를 refs 디렉토리에 저장하고 요소 id로 등록(파일 연결) —
    co-writer-bot의 _auto_register_element 저장 로직과 동일(data/refs/<work>/<name>.png)."""
    dest_dir = oi.config.OPENROUTER_REFS_DIR / oi.canon_work(work)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_element_filename(name)}.png"
    (dest_dir / filename).write_bytes(png)
    oi.register_element(work, name, etype=etype, filename=filename, aliases=[name])


def _safe_element_filename(name: str) -> str:
    return re.sub(r"[^\w가-힣.\-]+", "_", name or "요소").strip("_.") or "요소"


# 씬들이 병렬로 돌면서 각자 fix_element_references를 호출하는데, 그 순간 "아직 안 고정된
# 요소" 목록이 겹치면 같은 장소/의상/소품 이미지를 중복 생성하는 경합이 생긴다(시간·비용 낭비).
# 프로세스 전역에서 "지금 생성 중인 요소"를 표시해두고 다른 씬은 건너뛰게 한다.
_INFLIGHT_ELEMENTS: set[tuple[str, str]] = set()
_INFLIGHT_LOCK = threading.Lock()


def fix_element_references(work: str, mood: str = "", conti_full: str = "") -> dict:
    """등록됐지만 아직 레퍼런스 이미지가 없는 장소·의상·소품에 대해 고정 이미지를 생성·등록한다.
    인물(person)은 초상화로 이미 얼굴 레퍼런스가 있으므로 건너뛴다. 반환: {등록됨, 실패} 개수.
    실패한 요소는 건너뛰고 계속 진행(전체 실패 방지).
    ★이미지 생성(oi.generate)은 건당 수십 초가 걸려서, 씬 하나에 고정할 요소가 여러 개면
    순차 호출이 그 씬 전체를 몇 분씩 묶어뒀다 — 요소별로 병렬 생성해 이 단계를 크게 줄인다.
    또한 병렬로 도는 다른 씬과 같은 요소를 동시에 중복 생성하지 않도록 _INFLIGHT_ELEMENTS로
    선점 표시를 남긴다(선점 못 한 요소는 이 호출에서 건너뛰고, 그 요소를 먼저 잡은 다른 씬의
    fix_element_references가 끝나면 이후 씬에서는 이미 file이 채워져 자연히 스킵된다)."""
    candidates = []
    with _INFLIGHT_LOCK:
        for e in oi.load_elements(work):
            etype = e.get("type")
            if etype not in ("place", "costume", "prop"):
                continue
            if e.get("file"):
                continue  # 이미 레퍼런스 이미지 있음
            name = e.get("display") or ""
            if not name:
                continue
            key = (work, name)
            if key in _INFLIGHT_ELEMENTS:
                continue  # 다른 씬이 지금 이 요소를 생성 중 — 중복 생성 방지, 스킵
            _INFLIGHT_ELEMENTS.add(key)
            candidates.append((name, etype))

    if not candidates:
        return {"registered": 0, "failed": 0}

    def _one(item):
        name, etype = item
        try:
            prompt = _element_ref_prompt(name, etype, mood=mood, context=conti_full)
            # 요소 참조 이미지는 스틸컷보다 작아도 되는데 기존 1:1 매핑(1024x1024)이 오히려
            # 스틸(720x1280, 92만px)보다 컸다(105만px) — gpt-image가 실제로 허용하는 최소
            # 픽셀 예산 근처인 832x832(16의 배수, 실측 확인)로 낮춰 생성 속도·비용을 줄인다.
            png, _cost = _with_retry(oi.generate, prompt, size="832x832", refs=[])
            _register_element_image(work, name, etype, png)
            return True
        except Exception:
            return False
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT_ELEMENTS.discard((work, name))

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(candidates), 4)) as ex:
        results = list(ex.map(_one, candidates))
    registered = sum(1 for r in results if r)
    return {"registered": registered, "failed": len(results) - registered}


def generate_shots_by_scene(scenes: list[tuple[int, str, str]], work: str | None = None,
                            characters: list[dict] | None = None) -> dict[int, list[dict]]:
    """씬별 상세콘티 body를 샷 단위로 분해. 반환: {씬번호: [shot dict, ...]}.
    work가 주어지면 등록된 장소·소품·의상 목록을 프롬프트에 같이 내려 컷마다 정식 이름/묘사가
    반복 재사용되게 한다(요소 레지스트리 — extract_and_register_elements가 미리 채워둔 것).
    OPENROUTER_API_KEY 필요(agent 백엔드 아니라 OpenRouter chat 사용, 원본과 동일)."""
    elems = oi.load_elements(work) if work else []
    places = sorted({e["display"] for e in elems if e.get("type") == "place"})
    props = sorted({e["display"] for e in elems if e.get("type") == "prop"})
    costumes = sorted({e["display"] for e in elems if e.get("type") == "costume"})
    system = sb_prompts.storyboard_shots_system(bible=characters_bible(characters),
                                                places=places or None,
                                                props=props or None, costumes=costumes or None)
    shots_by_scene = {}
    for num, _hdr, body in scenes:
        raw = _with_retry(oi.chat, system, sb_prompts.storyboard_shots_user(body))
        shots = [s for s in parsing.parse_json_array(raw) if s.get("prompt")]
        for i, s in enumerate(shots, 1):
            s["n"] = i
        shots_by_scene[num] = shots
    return shots_by_scene


# 컷 스틸·영상 화풍을 세미리얼리스틱으로 통일 — 샷 프롬프트에 매번 붙인다(영상은 이 스틸을
# 입력으로 쓰므로 영상 화풍도 자동으로 따라온다). 초상화·요소 레퍼런스도 같은 세미리얼 계열이라
# 전 단계 화풍이 일치한다.
SEMI_REAL_SUFFIX = (
    " Semi-realistic cinematic style, roughly 80% realism — photoreal lighting, proportion, and "
    "depth with a subtly stylized painterly/illustrated finish. Not a flat cartoon or anime, and "
    "not a pure photograph. No text, letters, captions, or watermark."
    # ★2026-07-21(실측 — 컷마다 같은 인물의 헤어스타일(묶음/풀림 등)·의상 디테일이 제멋대로
    # 바뀜): 인물/의상 참조 이미지의 role instruction만으로는 힘이 부족해, 프롬프트 본문 끝에도
    # 같은 지시를 명시적으로 반복해 앵커를 강화한다.
    " Keep this character's hairstyle (including whether hair is tied up or down) and every "
    "costume detail (color, fit, accessories) IDENTICAL to their reference image in every shot — "
    "do not alter hair state or outfit details unless the action/prompt explicitly describes a change."
)


def generate_image_for_shot(shot: dict, work: str | None = None) -> tuple[bytes, float]:
    """샷 하나의 스틸컷 생성 → (PNG bytes, cost$). work가 주어지면 요소 레지스트리(인물 얼굴·
    장소·소품·의상 참조 이미지)를 shot_refs_with_instructions()로 자동 매칭해 일관성을 유지한다.
    ★2026-07-21: 예전엔 oi.shot_refs()로 참조 URL만 붙이고 각 참조가 얼굴용인지 의상용인지
    설명이 전혀 없었다(_ROLE_INSTRUCTIONS/shot_ref_entries가 정의만 되고 실제로는 아무 데도
    안 쓰이던 죽은 코드였음, 실측 확인) — 생성기가 참조를 뒤섞어 써서 인물 얼굴·의상·헤어가
    컷마다 흔들리는 원인이었다. 이제 참조별 역할 설명을 프롬프트 본문에 번호 붙여 명시한다.
    프롬프트 끝에 세미리얼리스틱 화풍 지시를 붙여 컷마다 톤이 흔들리지 않게 한다."""
    ref_instructions, refs = oi.shot_refs_with_instructions(work, shot) if work else ("", [])
    prompt = f"{shot['prompt']}{SEMI_REAL_SUFFIX}"
    if ref_instructions:
        prompt = f"{ref_instructions}\n\n{prompt}"
    return _with_retry(oi.generate, prompt, aspect_ratio="9:16", refs=refs)


_ANIME_CASCADE = os.path.join(os.path.dirname(__file__), "lbpcascade_animeface.xml")


def _iou(a, b) -> float:
    ax0, ay0, aw, ah = a; bx0, by0, bw, bh = b
    ax1, ay1, bx1, by1 = ax0 + aw, ay0 + ah, bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def _merge_boxes(boxes: list[tuple[int, int, int, int]], iou_thresh: float = 0.3) -> list[tuple[int, int, int, int]]:
    """서로 많이 겹치는 박스(같은 얼굴을 여러 캐스케이드가 중복 감지)만 하나로 합친다."""
    merged: list[tuple[int, int, int, int]] = []
    for b in boxes:
        hit = next((i for i, m in enumerate(merged) if _iou(m, b) > iou_thresh), None)
        if hit is None:
            merged.append(b)
        else:
            mx0, my0, mw, mh = merged[hit]
            bx0, by0, bw, bh = b
            x0, y0 = min(mx0, bx0), min(my0, by0)
            x1, y1 = max(mx0 + mw, bx0 + bw), max(my0 + mh, by0 + bh)
            merged[hit] = (x0, y0, x1 - x0, y1 - y0)
    return merged


def _detect_face_boxes(png: bytes, W: int, H: int) -> list[tuple[int, int, int, int]]:
    """cv2로 화면에 보이는 얼굴 전부를 (x,y,w,h) 리스트로 감지. 애니풍 → 실사(정면+측면 양방향)
    순으로 시도하고, 못 찾거나 cv2가 없으면 상단 중앙 휴리스틱 박스 하나로 폴백한다(세로 컷은
    얼굴이 보통 위쪽). 2인 이상 등장 컷에서 얼굴 하나만 가리면 남은 얼굴 때문에 안전필터가
    여전히 걸리므로 감지된 얼굴 전부를 반환한다(2026-07-21, 실측 — 2인 컷에서 격자 재시도도
    필터에 걸림).
    ★2026-07-21: 정면 캐스케이드만으로는 반측면·측면으로 돌아선 얼굴을 놓친다(실측 — 2인 컷에서
    한쪽 인물이 반측면이라 얼굴 감지가 안 돼 격자가 안 씌워짐 → 필터 재발). haarcascade_profileface
    (왼쪽 측면 기준으로 학습됨)를 원본 + 좌우반전 이미지 양쪽에 돌려 오른쪽/왼쪽 측면 얼굴을 모두
    잡고, 정면 결과와 합친다(같은 얼굴 중복 검출은 IoU 병합으로 정리)."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            min_size = (int(W * 0.08), int(W * 0.08))
            cands = []
            if os.path.exists(_ANIME_CASCADE):
                cands = list(cv2.CascadeClassifier(_ANIME_CASCADE).detectMultiScale(
                    gray, 1.1, 5, minSize=min_size))
            if len(cands) == 0:
                frontal = cv2.CascadeClassifier(
                    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                ).detectMultiScale(gray, 1.1, 4, minSize=min_size)
                cands.extend(frontal)
                profile_clf = cv2.CascadeClassifier(
                    cv2.data.haarcascades + "haarcascade_profileface.xml")
                cands.extend(profile_clf.detectMultiScale(gray, 1.1, 4, minSize=min_size))
                flipped = cv2.flip(gray, 1)
                for x, y, w, h in profile_clf.detectMultiScale(flipped, 1.1, 4, minSize=min_size):
                    cands.append((W - x - w, y, w, h))  # 좌우반전 좌표를 원본 좌표로 복원
            if len(cands) > 0:
                boxes = []
                for x, y, w, h in cands:
                    px, py = int(w * 0.03), int(h * 0.03)
                    x0 = max(0, x - px); y0 = max(0, y - py)
                    x1 = min(W, x + w + px); y1 = min(H, y + h + py)
                    boxes.append((x0, y0, x1 - x0, y1 - y0))
                return _merge_boxes(boxes)
    except Exception:
        pass
    # 폴백: 상단 중앙 박스 하나
    w, h = int(W * 0.65), int(H * 0.36)
    return [((W - w) // 2, int(H * 0.10), w, h)]


def _facegrid_overlay(png: bytes) -> bytes:
    """image-to-video의 실존인물 안전필터(InputImageSensitiveContentDetected)를 회피하려고
    화면에 보이는 얼굴 전부에 빨간 3×3 격자를 얹는다(2인 이상 등장 컷에서 하나만 가리면 남은
    얼굴 때문에 필터가 여전히 걸림 — 2026-07-21 실측). cv2로 얼굴을 자동 감지(애니풍→실사정면→
    상단중앙 폴백)해 그 위에 격자를 그린다 — 필터는 얼굴이 가려지면 통과한다."""
    from PIL import Image, ImageDraw
    base = Image.open(io.BytesIO(png)).convert("RGBA")
    W, H = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for x, y, w, h in _detect_face_boxes(png, W, H):
        x0, y0, x1, y1 = x, y, x + w, y + h
        for i in range(4):
            gx = round(x0 + (x1 - x0) * i / 3)
            d.line([(gx, y0), (gx, y1)], fill=(237, 28, 36, 255), width=max(2, W // 200))
            gy = round(y0 + (y1 - y0) * i / 3)
            d.line([(x0, gy), (x1, gy)], fill=(237, 28, 36, 255), width=max(2, W // 200))
    out = io.BytesIO()
    Image.alpha_composite(base, overlay).convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _trim_head_0_1s(path: str, seconds: float = 0.1, timeout: int = 60) -> bool:
    """저장된 mp4의 맨 앞 `seconds`초를 잘라 같은 경로에 덮어쓴다(격자 첫 프레임이 최종 영상에
    안 비치게). 프레임 정확도를 위해 재인코딩. 실패해도 원본은 그대로 두고 False 반환."""
    import subprocess
    import pathlib
    src = pathlib.Path(path)
    if not src.exists():
        return False
    tmp = src.with_name(src.stem + "_trim" + src.suffix)
    cmd = [sb_config.FFMPEG_BIN, "-y", "-ss", str(seconds), "-i", str(src),
           "-map", "0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            if tmp.exists():
                tmp.unlink()
            return False
        os.replace(tmp, src)
        return True
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        return False


# image-to-video 안전필터가 "실존 인물"로 오판할 때, 컷 이미지를 명백한 2D 일러스트/애니 화풍으로
# 다시 그려 재시도한다 — 포토리얼 얼굴을 빼면 필터가 통과한다(데모 우선; 그 컷만 화풍이 달라짐).
def generate_video_for_cut(work: str, scene_num: int, cut_num: int, png: bytes,
                           motion_prompt: str, episode: int = 1, shot: dict | None = None) -> str:
    """스틸컷 1장을 영상화해 로컬 mp4 절대경로를 반환. project_setup.ensure_project(work)를
    먼저 호출해둬야 vp_store가 프로젝트 디렉토리를 찾을 수 있다.
    ★실존인물 안전필터에 걸리면 재스타일화 없이 곧바로 얼굴에 빨간 격자를 덮어(cv2 얼굴 자동
    감지) 재시도한다. 격자 회피 시엔 그 격자 스틸이 승인된 시작 프레임임을 프롬프트 맨 앞줄에
    못박고(anchor), 생성된 영상 앞 0.1초를 잘라 격자 첫 프레임이 최종 영상에 안 비치게 한다."""
    def _gen_once(image_bytes, prompt):
        # _with_retry 재시도 없이 1회만 — 안전필터는 재시도해도 같은 결과라 낭비
        return hf_video.generate(image_bytes, prompt,
                                 aspect_ratio="9:16", generate_audio=False)

    def _is_filter(e):
        return "InputImageSensitiveContentDetected" in str(e)

    grid_used = False
    try:
        url, cost = _gen_once(png, motion_prompt)
    except Exception as e:
        if not _is_filter(e):
            raise
        # 안전필터 → 얼굴 격자로 재시도
        grid_anchor = (
            f"<<<cut{cut_num}.png>>> is the clean approved start frame and must remain the exact "
            f"identity, costume, location, lighting, and screen-direction anchor.\n")
        try:
            url, cost = _gen_once(_facegrid_overlay(png), grid_anchor + motion_prompt)
        except Exception as e2:
            if _is_filter(e2):
                raise RuntimeError(
                    f"안전필터 회피 실패 (씬{scene_num} 컷{cut_num}): 얼굴 격자 적용 후에도 필터에 걸렸습니다."
                ) from e2
            raise
        grid_used = True
    path = vp_store.save_video(work, scene_num=scene_num, cut_num=cut_num, url=url,
                               episode=episode, cost=cost)
    if not path:
        raise RuntimeError(f"영상 다운로드/저장 실패 (씬{scene_num} 컷{cut_num})")
    # 격자로 생성한 경우 격자 첫 프레임이 최종 영상에 비치지 않도록 앞 0.1초 트림
    if grid_used:
        _trim_head_0_1s(path, 0.1)
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
            png, _img_cost = generate_image_for_shot(shot, work=work)
            # 영상화가 실패해도(안전필터 등) 이미 생성한 스틸은 남겨 재생성 비용 낭비를 막는다.
            try:
                vp_store.save_still(work, scene_num=scene_num, prompt_summary=shot.get("prompt", ""),
                                    png=png, cuts=[{**shot, "png": png}], episode=episode)
            except Exception:
                pass
            notify("영상화 중")
            path = generate_video_for_cut(work, scene_num, cut_num, png,
                                          motion_prompt=shot.get("caption", shot["prompt"]),
                                          episode=episode, shot=shot)
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


def _norm_scenes(scenes):
    """JSON 왕복으로 튜플→리스트가 된 scenes를 (int, str, str) 튜플로 정규화."""
    return [(int(s[0]), s[1], s[2]) for s in scenes]


def _norm_shots(sbs):
    """JSON 왕복으로 문자열이 된 shots_by_scene 키를 int로 정규화."""
    return {int(k): v for k, v in (sbs or {}).items()}


def _project_mood(project: dict) -> str:
    return " · ".join(x for x in [project.get("logline", ""),
                                  (project.get("synopsis") or "")[:150]] if x)


def _prepare_scenes_and_shots(project, episode, job_id, save_fn=None):
    """이 화의 (scenes, shots_by_scene)를 준비. 이미 만들어 저장돼 있으면(스틸 미리보기에서 생성)
    그대로 재사용해 두 번 만들지 않는다 — 미리보기 스틸과 실제 영상의 컷이 일치하게. 없으면
    씬설계→콘티→요소 등록·고정→샷분해를 새로 만들고 save_fn으로 화에 저장한다."""
    work = project["work"]
    num = episode["num"]
    characters = project.get("characters", [])
    if episode.get("scenes") and episode.get("shots_by_scene"):
        return _norm_scenes(episode["scenes"]), _norm_shots(episode["shots_by_scene"])

    script = episode.get("script")
    if not script:
        raise RuntimeError("대본이 먼저 있어야 해요.")
    jobs.update(job_id, stage="씬 설계 중")
    plan_text = generate_scene_plan(script, episode=num, characters=characters)
    scenes_plan = parsing.parse_plan_scenes(plan_text)
    if not scenes_plan:
        raise RuntimeError("씬 설계안에서 씬 목록을 파싱하지 못했어요.")
    conti_full = generate_conti(script, plan_text, scenes_plan, episode=num, characters=characters)
    scenes = parsing.split_scenes(conti_full)
    if not scenes:
        raise RuntimeError("콘티에서 씬 헤더를 찾지 못했어요.")
    try:
        extract_and_register_elements(work, conti_full)
    except Exception:
        pass
    # 장소·의상·소품도 얼굴처럼 고정 레퍼런스 이미지를 만들어 요소 id로 등록 — 이후 샷 생성이
    # shot_refs로 이 이미지들을 물어 배경·의상이 컷마다 일관되게 유지된다.
    jobs.update(job_id, stage="배경·의상 고정 중")
    try:
        fix_element_references(work, mood=_project_mood(project), conti_full=conti_full)
    except Exception:
        pass
    jobs.update(job_id, stage="샷 분해 중")
    shots_by_scene = generate_shots_by_scene(scenes, work=work, characters=characters)
    if save_fn:
        save_fn(plan_text=plan_text, conti_full=conti_full, scenes=scenes,
                shots_by_scene=shots_by_scene)
    return scenes, shots_by_scene


def _representative_shot(shots: list[dict]) -> dict | None:
    """씬의 '주요 장면' 스틸용 대표 샷 — 등장인물이 가장 많은(핵심 상호작용) 샷, 동률이면 첫 샷."""
    if not shots:
        return None
    return max(shots, key=lambda s: (len(s.get("characters") or []), -s.get("n", 0)))


def _cut_stills_from_shots(work: str, scene_num: int, shots: list[dict]) -> list[dict]:
    """씬의 컷(샷) 전부에 대해 스틸을 1장씩 생성 → [{"scene_num","cut_num","caption","prompt",
    "image","video_path"}...] (실패한 컷은 건너뜀). ★2026-07-21(사용자 지시 — 씬 대표 1컷만이
    아니라 모든 컷을 미리보기에서 보여주고, 각 컷마다 재생성·영상화를 개별로 할 수 있게):
    기존엔 씬당 대표 샷 1장만 만들었지만, 이제 컷 단위로 재생성/영상화 버튼을 붙여야 해서
    씬의 모든 컷 이미지를 다 만든다."""
    def _one(shot):
        try:
            png, _cost = generate_image_for_shot(shot, work=work)
        except Exception:
            return None
        return {"scene_num": scene_num, "cut_num": shot.get("n"), "caption": shot.get("caption", ""),
                "prompt": shot.get("prompt", ""), "image": oi.png_data_url(png), "video_path": None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(shots) or 1, 3)) as ex:
        results = [r for r in ex.map(_one, shots) if r]
    results.sort(key=lambda r: r["cut_num"] or 0)
    return results


def generate_stills_for_scene(project: dict, episode: dict, job_id: str, save_fn=None,
                              scene_num: int = 1) -> None:
    """영상 만들기 전 미리보기 — ★2026-07-21(사용자 지시 — 한 번에 전체 씬을 다 만들지 말고,
    1씬만 먼저 만든 뒤 사용자가 원할 때마다 다음 씬을 하나씩 만들게): scene_num으로 지정한
    씬 하나만 콘티→요소등록·고정→샷분해→컷별 스틸까지 처리한다.
    아직 씬 목록(scene_lines) 자체가 없으면(이 화의 첫 미리보기 요청) 씬 설계부터 한 번만 하고,
    그 뒤로는 매 호출마다 스킵한다(이미 있으면 재사용). 이미 처리된 씬을 다시 요청하면 그대로
    완료 처리하고 새로 만들지 않는다(중복 방지)."""
    work = project["work"]
    num = episode["num"]
    characters = project.get("characters", [])
    mood = _project_mood(project)
    try:
        project_setup.ensure_project(work)

        scene_lines = episode.get("scene_lines")
        plan_text = episode.get("plan_text", "")
        if not scene_lines:
            script = episode.get("script")
            if not script:
                raise RuntimeError("대본이 먼저 있어야 해요.")
            jobs.update(job_id, stage="씬 설계 중")
            plan_text = generate_scene_plan(script, episode=num, characters=characters)
            scenes_plan = parsing.parse_plan_scenes(plan_text)
            if not scenes_plan:
                raise RuntimeError("씬 설계안에서 씬 목록을 파싱하지 못했어요.")
            scene_lines = [[n, line] for n, line in scenes_plan]
            if save_fn:
                save_fn(plan_text=plan_text, scene_lines=scene_lines)

        total = len(scene_lines)
        jobs.update(job_id, total=total)

        existing_scenes = {s[0]: s for s in (episode.get("scenes") or [])}
        if scene_num in existing_scenes:
            # 이미 처리된 씬 — 다시 만들지 않고 그대로 완료 처리(중복 요청 방지).
            jobs.update(job_id, status="done", stage="완료",
                       stills=episode.get("scene_stills") or [])
            return

        line = next((l for n, l in scene_lines if n == scene_num), None)
        if line is None:
            raise RuntimeError(f"씬{scene_num}을 찾을 수 없어요.")
        script = episode.get("script")
        jobs.update(job_id, stage=f"씬{scene_num} 콘티 작성 중")
        conti = generate_conti(script, plan_text, [(scene_num, line)], episode=num,
                               characters=characters)
        sc = parsing.split_scenes(conti)
        if not sc:
            raise RuntimeError(f"씬{scene_num} 콘티에서 씬 헤더를 찾지 못했어요.")
        try:
            extract_and_register_elements(work, conti)
        except Exception:
            pass
        try:
            fix_element_references(work, mood=mood, conti_full=conti)
        except Exception:
            pass
        jobs.update(job_id, stage=f"씬{scene_num} 샷 분해 중")
        shots_by = generate_shots_by_scene(sc, work=work, characters=characters)
        scene_tuple = sc[0]
        jobs.update(job_id, stage=f"씬{scene_num} 스틸 생성 중")
        cuts = _cut_stills_from_shots(work, scene_num, shots_by.get(scene_tuple[0]) or [])

        scenes = list(episode.get("scenes") or []) + [list(scene_tuple)]
        scenes.sort(key=lambda s: s[0])
        shots_by_scene = dict(episode.get("shots_by_scene") or {})
        shots_by_scene.update(shots_by)
        stills = list(episode.get("scene_stills") or []) + cuts
        stills.sort(key=lambda c: (c["scene_num"], c.get("cut_num") or 0))
        conti_full = episode.get("conti_full") or ""
        conti_full = (conti_full + "\n\n" + conti).strip() if conti_full else conti

        if save_fn:
            save_fn(scenes=scenes, shots_by_scene=shots_by_scene, scene_stills=stills,
                    conti_full=conti_full)
        jobs.update(job_id, status="done", stage="완료", stills=stills)
    except Exception as e:
        jobs.update(job_id, status="error", stage="오류", error=str(e))


def regenerate_cut_still(project: dict, episode: dict, scene_num: int, cut_num: int) -> dict:
    """미리보기의 특정 컷 이미지만 다시 생성 → 갱신된 still 항목(dict) 반환."""
    work = project["work"]
    shots_by_scene = _norm_shots(episode.get("shots_by_scene") or {})
    shot = next((s for s in (shots_by_scene.get(scene_num) or []) if s.get("n") == cut_num), None)
    if not shot:
        raise RuntimeError(f"씬{scene_num} 컷{cut_num}을 찾을 수 없어요.")
    png, _cost = generate_image_for_shot(shot, work=work)
    return {"scene_num": scene_num, "cut_num": cut_num, "caption": shot.get("caption", ""),
            "prompt": shot.get("prompt", ""), "image": oi.png_data_url(png), "video_path": None}


def videoize_cut_job(project: dict, episode: dict, scene_num: int, cut_num: int, job_id: str) -> None:
    """미리보기(또는 재생성)로 만들어둔 특정 컷 스틸을 그대로 영상화. 백그라운드 스레드 전제
    (예외는 jobs에 error로 남김) — server.py가 threading.Thread로 호출."""
    work = project["work"]
    num = episode["num"]
    try:
        project_setup.ensure_project(work)
        shots_by_scene = _norm_shots(episode.get("shots_by_scene") or {})
        shot = next((s for s in (shots_by_scene.get(scene_num) or []) if s.get("n") == cut_num), None)
        if not shot:
            raise RuntimeError(f"씬{scene_num} 컷{cut_num}을 찾을 수 없어요.")
        still = next((s for s in (episode.get("scene_stills") or [])
                     if s.get("scene_num") == scene_num and s.get("cut_num") == cut_num and s.get("image")),
                    None)
        if not still:
            raise RuntimeError("이 컷의 스틸이 없어요. 먼저 이미지를 만들어주세요.")
        png = oi.data_url_to_png(still["image"])
        jobs.update(job_id, stage="영상화 중")
        path = generate_video_for_cut(work, scene_num, cut_num, png,
                                      motion_prompt=shot.get("caption", shot["prompt"]),
                                      episode=num, shot=shot)
        jobs.update(job_id, status="done", stage="완료", video_path=path)
    except Exception as e:
        jobs.update(job_id, status="error", stage="오류", error=str(e))


def produce_episode_video(project: dict, episode: dict, job_id: str, save_fn=None) -> None:
    """스튜디오 화 하나를 영상으로 제작 — 스틸 미리보기에서 만든 씬·샷이 있으면 재사용, 없으면
    새로 만들고 이미지→영상→합본까지. 백그라운드 스레드 전제(예외는 jobs에 error로 남김)."""
    work = project["work"]
    num = episode["num"]
    idea = project.get("idea") or project.get("logline") or ""
    try:
        project_setup.ensure_project(work)
        if not episode.get("script"):
            raise RuntimeError("대본이 먼저 있어야 영상을 만들 수 있어요.")
        scenes, shots_by_scene = _prepare_scenes_and_shots(project, episode, job_id, save_fn=save_fn)

        # 데모 모드: 첫 씬의 첫 컷만 영상화해서 그 영상을 결과로 보여준다(합본 생략).
        # 전 컷 영상화는 시간·비용이 크고 안전필터 리스크가 있어, 데모에선 "영상까지 이어진다"만
        # 컷1로 증명한다. DEMO_FIRST_CUT_ONLY=0 으로 끄면 전체 컷+합본 경로로 돌아간다.
        if os.environ.get("DEMO_FIRST_CUT_ONLY", "1") != "0":
            scene_stills = episode.get("scene_stills") or []
            # ★2026-07-21(사용자 지시 — "미리보기에 안 나오는 장면은 이미지로 만들지 마라"):
            # 미리보기(generate_scene_stills)에 이미 뜬 씬만 후보로 삼는다. 새로 이미지를 뽑는
            # 폴백은 없앤다 — 미리보기에 없는 씬은 영상화 대상에서 아예 제외.
            candidates = []
            for sn, _h, _b in scenes:
                shots = shots_by_scene.get(sn) or []
                rep = _representative_shot(shots) or (shots[0] if shots else None)
                # ★2026-07-21: 미리보기가 이제 씬당 여러 컷을 만들어두므로(scene_stills에 컷별
                # 항목이 여러 개), 대표 샷과 같은 cut_num의 스틸을 정확히 찾아야 한다 — 그냥
                # scene_num만 맞는 첫 항목을 집으면 대표 샷이 아닌 다른 컷이 걸릴 수 있다.
                still = next((s for s in scene_stills
                             if s.get("scene_num") == sn and s.get("cut_num") == (rep or {}).get("n")
                             and s.get("image")), None)
                if shots and rep and still:
                    candidates.append((sn, shots, rep, still))
            if not candidates:
                raise RuntimeError("미리보기에 만들어진 스틸이 없어 영상을 만들 수 없어요. "
                                   "먼저 '장면 미리보기'로 스틸컷을 만들어주세요.")
            scene_num, shots, shot, cached_still = candidates[0]
            jobs.update(job_id, stage="미리보기 스틸 재사용 중")
            png = oi.data_url_to_png(cached_still["image"])
            # 영상화가 실패해도(안전필터 등) 이미 생성한 스틸은 남겨 재생성 비용 낭비를 막는다.
            try:
                vp_store.save_still(work, scene_num=scene_num, prompt_summary=shot.get("prompt", ""),
                                    png=png, cuts=[{**shot, "png": png}], episode=num)
            except Exception:
                pass
            jobs.update(job_id, stage="컷1 영상화 중")
            path = generate_video_for_cut(work, scene_num, shot["n"], png,
                                          motion_prompt=shot.get("caption", shot["prompt"]),
                                          episode=num, shot=shot)
            jobs.update(job_id, status="done", stage="완료(데모: 컷1만)", video_path=path)
            return

        total_shots = sum(len(s) for s in shots_by_scene.values())
        done = 0

        def on_progress(scene_num, cut_num, msg):
            nonlocal done
            if msg == "완료" or msg.startswith("실패"):
                done += 1
            jobs.update(job_id, stage=f"영상 제작 중 ({done}/{total_shots}컷) — 씬{scene_num} 컷{cut_num}: {msg}")

        for scene_num, _hdr, _body in scenes:
            shots = shots_by_scene.get(scene_num) or []
            if shots:
                generate_cuts_for_scene(work, scene_num, shots, episode=num, on_progress=on_progress)

        jobs.update(job_id, stage="합본 중")
        title = f"{num}화" + (f" — {episode['subtitle']}" if episode.get("subtitle") else "")
        draft_path = compile_episode_video(work, idea, scenes, shots_by_scene,
                                           episode=num, episode_title=title)
        jobs.update(job_id, status="done", stage="완료", video_path=draft_path)
    except Exception as e:
        jobs.update(job_id, status="error", stage="오류", error=str(e))
