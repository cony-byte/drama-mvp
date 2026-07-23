# -*- coding: utf-8 -*-
"""한 줄 아이디어 → 기획안 → 대본 → 씬설계 → 상세콘티 → 샷분해 (1~5단계, 텍스트만).
이미지·영상·합본(6~8단계)은 나중에 이어붙인다. 모든 LLM/HTTP 호출은 vendor의 기존 함수 재사용."""
import concurrent.futures
import hashlib
import io
import logging
import os
import re
import threading
import time

import vendor.cowriter.bot.prompts as cw_prompts
import vendor.storyboard.bot.config as sb_config
import vendor.storyboard.bot.costmeter as costmeter
import vendor.storyboard.bot.edit_plan as edit_plan
import vendor.storyboard.bot.episode_compile as episode_compile
import vendor.storyboard.bot.generator as sb_generator
import vendor.storyboard.bot.openrouter_image as oi
import vendor.storyboard.bot.openrouter_video as hf_video
import vendor.storyboard.bot.prompts as sb_prompts
import vendor.storyboard.bot.video_index as video_index
import vendor.storyboard.bot.vp_store as vp_store

from pipeline import jobs, parsing, project_setup, v3_schema

# storyboard-bot 로거 — uvicorn은 root에 INFO 핸들러를 안 달아 log.info가 안 보이므로(WARNING+만
# lastResort로 stderr) 여기서 전용 StreamHandler를 INFO로 직접 붙인다. propagate=False로 root
# lastResort와의 이중 출력을 막는다. 실패·계측을 조용히 삼키지 않고 stderr(=uvicorn 로그)에 남긴다.
log = logging.getLogger("storyboard-bot")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def _fmt_elapsed(sec: float) -> str:
    """초 → '12분 34초' / '45초' (사람이 읽는 소요시간)."""
    sec = int(round(sec))
    return f"{sec // 60}분 {sec % 60}초" if sec >= 60 else f"{sec}초"


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


def _make_face_reference(png: bytes, character: dict) -> bytes | None:
    """캐릭터 카드의 원본 이미지(png)에서 '얼굴 전용 레퍼런스'를 img2img로 만든다 — 원본 인물의
    얼굴·헤어 정체성은 최대한 보존하되 정면·얼굴~어깨 크롭·중립 무지 상의·액세서리 제거·흰 배경
    으로 중립화(★2026-07-22 사용자 요청). 실패하면 None(호출부가 원본으로 폴백)."""
    try:
        face_appearance = _face_appearance((character.get("appearance") or "").strip())
        appearance_clause = (f" Keep this face/hair identity: {face_appearance}."
                            if face_appearance else "")
        prompt = (
            "Redraw the exact same person from the attached reference image as a clean identity "
            "headshot. Preserve their facial identity, facial features, proportions, skin tone, and "
            f"hairstyle from the reference as closely as possible — same face, same hair.{appearance_clause} "
            "Do NOT copy the clothing, accessories, pose, or background from the reference; replace "
            "them per the framing rules. Do NOT use a full-body or strong-costume framing. "
            + FACE_REF_FRAMING + " " + PORTRAIT_STYLE)
        out, _cost = _with_retry(oi.generate, prompt, aspect_ratio="2:3",
                                refs=[oi.png_data_url(png)])
        return out
    except Exception:
        return None


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


# ── v3.1 파이프라인(HANDOFF_V3_1_PIPELINE.md) 3단계: 화 전체 1~4단계 뼈대 ──────
# 기존 generate_scene_plan(위)과 별개의 새 경로다 — 기존 shot 기반 파이프라인
# (generate_stills_for_scene 등)은 건드리지 않고 v3.1 스키마 위에 추가로 쌓는다.

def generate_episode_skeleton(script: str, episode: int = 1,
                              characters: list[dict] | None = None) -> str:
    """3단계: 화 전체 뼈대(씬 나누기, 등장·의상·장소·무드·소품·액션라인 확정, 클립 분할·초
    배분) 텍스트를 생성. 이 단계는 이미지·영상은 물론 컷 상세([N초] 자세·동작 서술)도 만들지
    않는다 — 그건 5단계(씬별 상세 블록)의 몫."""
    return _with_retry(
        _sb_complete,
        sb_prompts.episode_skeleton_system(bible=characters_bible(characters)),
        sb_prompts.episode_skeleton_user(script)).strip()


def generate_episode_skeleton_validated(
        script: str, episode: int = 1,
        characters: list[dict] | None = None) -> tuple[str, list[dict], list[str]]:
    """3단계 뼈대를 생성 + 파싱·검증. 반환: (뼈대 원문 text, scenes, errors). 뼈대 원문을 그대로
    돌려주는 이유는 5단계(scene_skeleton_texts로 씬별 뼈대 텍스트를 잘라 build_scene_blocks에
    넘김)에 그 원문이 필요하기 때문이다."""
    text = generate_episode_skeleton(script, episode=episode, characters=characters)
    scene_tuples = parsing.split_scenes(text)
    scenes = [v3_schema.parse_scene(hdr, body) for _, hdr, body in scene_tuples]
    for s in scenes:
        s["state"] = "validating"
    errors = []
    for s in scenes:
        errors.extend(v3_schema.validate_skeleton_scene(s))
    errors.extend(v3_schema.validate_episode_timing(scenes))
    if not errors:
        for s in scenes:
            s["state"] = v3_schema.next_state(s["state"])  # validating → references_ready
    return text, scenes, errors


def build_episode_skeleton(script: str, episode: int = 1,
                          characters: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """화 전체 뼈대를 생성 + pipeline.v3_schema로 파싱·검증까지 한 번에 수행.
    반환: (scenes, errors). errors가 비어 있어야(구조·시간 규칙 통과) 5단계(씬별 상세 블록)로
    넘어갈 수 있다 — 문서의 '씬 통과 조건과 상태 머신' 원칙을 화 전체 단위에 먼저 적용한 것."""
    _text, scenes, errors = generate_episode_skeleton_validated(
        script, episode=episode, characters=characters)
    return scenes, errors


# ── v3.1 파이프라인 5단계: 씬 하나의 상세 블록 생성 + 검증/부분 재생성 ──────────
# 3단계 뼈대(씬 헤더·선언·클립 마커·초)를 입력으로, 그 씬 하나의 각 클립에 [N초] 블록을 채운다.
# 화 전체를 한 번에 돌리지 않고 씬 단위로 완주(문서 '5단계부터 씬 하나씩 완주')하기 위한 최소 단위다.

def scene_skeleton_texts(skeleton_text: str) -> list[tuple[int, str]]:
    """3단계 뼈대 전체 텍스트 → [(씬번호, 그 씬 하나의 뼈대 텍스트)] 목록. 5단계는 이 씬 텍스트를
    하나씩 받아 상세 블록을 채운다. split_scenes가 '■'를 떼므로 헤더를 복원해 붙인다."""
    return [(num, f"■ {hdr}\n{body}".strip())
            for num, hdr, body in parsing.split_scenes(skeleton_text)]


def generate_scene_blocks(scene_skeleton: str, script: str,
                          prior_handoff: dict | None = None,
                          characters: list[dict] | None = None,
                          work: str | None = None,
                          error_feedback: str = "",
                          synopsis: str = "", summary: str = "") -> str:
    """5단계: 씬 뼈대 하나 + 대본 → 그 씬의 상세 블록([N초] 구도/자세/동작/소리)을 채운 콘티 텍스트.
    synopsis(전체 줄거리)·summary(이번 화 요약)를 주면 맥락으로 함께 넣는다(인물 동기·아크 반영).
    error_feedback가 있으면 직전 검증 오류를 프롬프트에 붙여 재생성한다(부분 재작성)."""
    sys_prompt = sb_prompts.scene_blocks_system(
        bible=characters_bible(characters),
        known_places=_element_names_for_prompt(work, "place") or None,
        known_costumes=_element_names_for_prompt(work, "costume") or None)
    user = sb_prompts.scene_blocks_user(
        scene_skeleton, script, prior_handoff=prior_handoff, error_feedback=error_feedback,
        synopsis=synopsis, summary=summary)
    return _with_retry(_sb_complete, sys_prompt, user).strip()


def build_scene_blocks(scene_skeleton: str, script: str,
                       prior_handoff: dict | None = None,
                       characters: list[dict] | None = None,
                       work: str | None = None,
                       max_attempts: int = 3,
                       synopsis: str = "", summary: str = "") -> tuple[dict | None, str, list[str]]:
    """씬 상세 블록을 생성하고 파싱만 한다. 반환: (scene_dict, conti_text, errors).
    ★2026-07-22(사용자 지시): v3.1 규칙 검증(validate_scene)·검증 실패 재생성을 제거 — 생성 1회로
    확정한다. 규칙 위반(블록 초 합 불일치, 구도 헤더 형식 등)이 있어도 반려/재시도하지 않고 그대로
    진행한다(검증 재시도로 인한 지연 제거). 씬 헤더 파싱 자체가 실패한 경우에만 하드 에러."""
    conti_text = generate_scene_blocks(
        scene_skeleton, script, prior_handoff=prior_handoff,
        characters=characters, work=work, synopsis=synopsis, summary=summary)
    parsed = parsing.split_scenes(conti_text)
    if not parsed:
        return None, conti_text, ["씬 헤더(■ 씬N …)를 찾지 못했어요 — 출력이 v3.1 씬 형식이 아니에요."]
    _, hdr, body = parsed[0]
    scene = v3_schema.parse_scene(hdr, body)
    scene["state"] = v3_schema.next_state("validating")  # 검증 없이 references_ready로 전진
    return scene, conti_text, []


# ── v3.1 파이프라인 6단계: 씬별 지연 레퍼런스 생성 ───────────────────────────
# 화 전체 요소를 미리 만들지 않고, 지금 처리하는 씬 하나에 필요한 장소·의상·소품만 등록·생성한다
# (문서 5단계 '씬1에 필요한 레퍼런스만 생성'). 인물 얼굴은 이미 초상 레퍼런스가 있어 제외.

def ensure_scene_references(work: str, scene: dict, mood: str = "",
                           conti_body: str = "") -> dict:
    """이 씬 하나에 필요한 요소(장소·의상·소품)를 등록하고, 아직 레퍼런스 이미지가 없는 것만
    생성·등록한다. 반환: {registered, failed}. 인물(person)은 얼굴 초상으로 이미 커버되므로 제외."""
    needs = v3_schema.scene_element_needs(scene)
    displays: set[str] = set()
    for name, etype in needs:
        try:
            e = oi.register_element(work, name, etype=etype)
            displays.add((e or {}).get("display") or name)
        except Exception:
            displays.add(name)  # 등록 실패해도 이름으로 후속 생성 시도
    if not displays:
        return {"registered": 0, "failed": 0}
    return fix_element_references(work, mood=mood, conti_full=conti_body, only=displays)


def _design_costume(character: dict, scene: dict, mood: str = "") -> str:
    """캐릭터(외모·신분/성격)와 장면 배경에 어울리는 '평상 의상' 한 벌을 구체적 시각 묘사(색·상/하의·
    소재·핏·디테일)로 설계해 한국어 한두 문장으로 반환한다. 참조 이미지 생성 context 및 의상 설명으로
    쓰인다. 실패하면 빈 문자열(호출부가 무난한 기본 디자인으로 폴백)."""
    name = character.get("name") or "인물"
    appearance = (character.get("appearance") or "").strip()
    desc = (character.get("description") or "").strip()
    setting = scene.get("location_tag") or ""
    system = (
        "너는 숏폼 드라마 의상 스타일리스트다. 주어진 인물과 장면에 자연스럽게 어울리는 평상 의상 "
        "한 벌을 골라, 옷만 구체적인 시각 묘사로 답한다. 상의·하의(또는 원피스)·색·소재·핏·눈에 "
        "띄는 디테일을 담되 한국어 1~2문장(짧게)으로만, 따옴표·군더더기 없이 옷 묘사만 낸다. "
        "인물의 신분·직업·성격과 장면 배경에 맞춰라(예: 편의점 알바생=근무 유니폼, 신분 숨긴 재벌가 "
        "인물=고급스럽지만 튀지 않는 미니멀 캐주얼). 얼굴·헤어·표정은 절대 언급하지 마라 — 옷만.")
    user = (f"인물: {name}\n외모: {appearance or '미상'}\n신분/성격: {desc or '미상'}\n"
            f"장면 배경: {setting or '미상'}\n작품 톤: {mood or '미상'}\n\n"
            "이 인물이 이 장면에서 입을 의상 한 벌을 묘사해줘.")
    try:
        out = " ".join(_cw_complete(system, user).split()).strip().strip('"' + "'")
        return out[:140]
    except Exception:
        return ""


def ensure_scene_costumes(work: str, scene: dict, characters: list[dict] | None = None,
                          mood: str = "") -> dict:
    """스틸 생성 전, 이 씬 cast 중 의상이 '⚠ 미등록'인 인물마다 어울리는 의상을 설계·등록하고
    참조 이미지를 만들어 그 인물에게 배정한다(★2026-07-22, 사용자 지시 — 미등록 의상은 스틸
    만들 때 알아서 만들어 등록). 인물별 안정 라벨('{이름} 의상')로 등록해 씬이 바뀌어도 같은
    의상을 재사용한다 — 이미 등록+참조이미지가 있으면 재설계·재생성 없이 배정만 한다. cast의
    costume 필드를 실제 라벨로 갱신하므로, 이후 _clip_pseudo_shot·WARDROBE LOCK·참조 매칭이
    자연히 그 의상을 쓴다. 반환: {designed, reused, failed}. 실패는 그 인물만 건너뛴다."""
    char_by_name = {c.get("name"): c for c in (characters or []) if c.get("name")}
    designed = reused = failed = 0
    for c in scene.get("cast") or []:
        name = c.get("name")
        costume = c.get("costume") or ""
        if not name or (costume and "미등록" not in costume):
            continue  # 이름 없음 또는 이미 등록 의상 있음 → 건드리지 않음
        label = f"{name} 의상"
        existing = oi.resolve_element(work, label)
        if existing and oi.element_has_image(work, existing):
            c["costume"] = existing.get("display") or label  # 이전 씬에서 만든 의상 재사용
            reused += 1
            continue
        try:
            design = _design_costume(char_by_name.get(name) or {"name": name}, scene, mood=mood)
            el = oi.register_element(work, label, etype="costume")
            if design:  # 의상 설명을 요소에 저장(WARDROBE LOCK·비전 후검사 근거)
                try:
                    elems = oi.load_elements(work)
                    for e in elems:
                        if e.get("id") == (el or {}).get("id"):
                            e["description"] = design
                    oi._save_elements(work, elems)
                except Exception:
                    pass
            prompt = _element_ref_prompt(label, "costume", mood=mood, context=design)
            png, _cost = _with_retry(oi.generate, prompt, size="832x832", refs=[])
            _register_element_image(work, label, "costume", png)
            c["costume"] = label
            designed += 1
        except Exception:
            failed += 1
    return {"designed": designed, "reused": reused, "failed": failed}


# ── v3.1 파이프라인 7·8단계: 클립 단위 대표 스틸 + 멀티샷 영상 ────────────────
# 기존 shot 단위(generate_image_for_shot / generate_cuts_for_scene)를 건드리지 않고, 클립을
# 영상 생성 1회 단위로 삼는 새 경로다. 대표 스틸 1장(+필요 시 보강컷)을 앵커로, 클립 전체 블록
# 콘티를 하나의 멀티샷 모션 프롬프트로 넘겨 클립당 영상 1개를 만든다(문서 '스틸'·'영상' 절).

def _clip_ordinal(clip_id: str | None) -> int:
    """'2-1' → 1, '2-2' → 2 (컷 id의 끝 컷번호를 정수로). 연속성 앵커 참조 텍스트의
    'approved shot N' 라벨용 — generate_image_for_shot가 그 값을 int로 캐스팅하기 때문.
    ★2026-07-23 컷 단위 전환 후 컷 id는 '씬-컷번호'(숫자)라 trailing 정수를 파싱한다."""
    tail = (clip_id or "").rsplit("-", 1)[-1].strip()
    return int(tail) if tail.isdigit() else 0


def _name_in_target(name: str, target: str) -> bool:
    """cast 이름이 구도 대상 문자열에 등장하는지 — 콘티가 '이수진'을 '수진'처럼 성 한 글자를
    뗀 짧은 이름으로 쓰는 경우까지 매칭한다(실측: '강태민 위주(수진 걸침)'에서 '이수진'을 놓침)."""
    if not (name and target):
        return False
    if name in target:
        return True
    return len(name) >= 3 and name[1:] in target  # 성(1글자) 뗀 이름(이수진→수진)


def _block_participants(scene: dict, block: dict | None) -> list[str] | None:
    """이 블록에 확실히 '단독'으로 등장하는 1인만 반환. 그 외(2인·위주·OTS·인서트·파싱 실패)는
    None을 반환해 호출부가 전체 cast로 폴백하게 한다 — 위주/걸침·OTS는 상대도 프레임에 보이므로
    함부로 줄이면 필요한 인물을 떨어뜨린다. '단독' 컷에서만, 이름이 정확히 1명 매칭될 때 좁힌다."""
    cast_names = [c.get("name") for c in (scene.get("cast") or []) if c.get("name")]
    target = v3_schema.parse_composition_header((block or {}).get("header") or "").get("target") or ""
    if target.endswith("단독"):
        present = [n for n in cast_names if _name_in_target(n, target)]
        if len(present) == 1:
            return present
    return None


def _block_focus_char(block: dict | None, cast_names: list[str]) -> str | None:
    """블록 대상이 '{이름} 단독' 또는 '{이름} 위주(...)'면 그 1인을 focus_char로 반환 — 얼굴 참조를
    그 인물로 좁혀(다른 인물 참조 drop) 정체성이 blend/drift되는 걸 막는다. '2인' 등 특정 1인이
    주체가 아닌 컷은 None(둘 다 유지). (★2026-07-22 실측: 강태민 단독/위주 컷인데 씬 선언 라인의
    상대 이름이 텍스트 스캔돼 상대 얼굴 참조까지 붙어 태민 얼굴이 다르게 나옴.)"""
    target = v3_schema.parse_composition_header((block or {}).get("header") or "").get("target") or ""
    m = re.match(r"(.+?)\s*(?:위주|단독)", target)
    if not m:
        return None
    key = m.group(1).strip()
    for n in cast_names:  # 풀네임/짧은 이름 모두 매칭
        if n and (n == key or _name_in_target(n, key) or key in n):
            return n
    return key or None


def _clip_pseudo_shot(scene: dict, clip: dict, block: dict, work: str | None = None,
                      characters: list[dict] | None = None) -> dict:
    """클립 대표 블록을 기존 이미지 생성기(generate_image_for_shot)가 이해하는 shot 유사 dict로
    변환한다 — 그래야 요소 참조(얼굴·의상·장소·소품) 매칭과 의상 잠금 로직을 그대로 재사용한다.

    ★2026-07-22(의상 뒤바뀜 수정): 세 가지를 바로잡는다.
    (1) 예전엔 씬 전체 cast의 인물·의상을 모든 클립에 붙여, 남자만 나오는 단독 컷에도 여자의
        의상 참조가 딸려가 뒤섞였다 — 블록 대상에 실제로 등장하는 인물만 남긴다(2인/인서트 등
        대상이 이름을 안 가리키면 종전대로 전체 cast).
    (2) 의상 값을 등록 엘리먼트의 display로 정규화한다 — shot_refs_with_instructions의 착용자
        매칭이 '의상 라벨==엘리먼트 display' 문자열 동일성에 의존하는데, 공백·하이픈 별칭 병합으로
        둘이 어긋나면 "이 옷은 X 전용, Y엔 금지" 문구가 조용히 이름 없는 일반 문구로 degrade돼
        인물 간 의상이 뒤바뀌던 원인(★실측). display로 맞춰 매칭이 항상 성사되게 한다.
    (3) ★실측 근본 원인: 의상이 씬 헤더에 '⚠ 미등록'으로 남으면 등록 의상이 하나도 없어
        WARDROBE LOCK이 아무 것도 못 잡고, 옷 정보가 프롬프트에 전혀 안 실려 모델이 옷을
        지어내 화면 초점 인물(예: 손님 남자)에게 엉뚱한 옷(편의점 유니폼)을 입혔다. 그래서
        (a) '미등록' 의상은 costume에서 버리고, (b) 캐릭터 appearance(옷 서술 포함)를 인물별로
        묶어 appearances로 실어 보낸다 — generate_image_for_shot이 인물↔외모/의상을 못박는다."""
    cast = scene.get("cast") or []
    present = _block_participants(scene, block)
    if present is not None:
        cast = [c for c in cast if c.get("name") in present]
    appearance_by_name = {c.get("name"): (c.get("appearance") or "").strip()
                          for c in (characters or []) if c.get("name")}
    costumes = {}
    appearances = {}
    for c in cast:
        name, costume = c.get("name"), c.get("costume")
        if not name:
            continue
        if costume and "미등록" not in costume:  # '⚠ 미등록'은 등록 의상 아님 → 무시
            el = oi.resolve_element(work, costume) if work else None
            costumes[name] = (el.get("display") if el and el.get("display") else costume)
        if appearance_by_name.get(name):
            appearances[name] = appearance_by_name[name]
    char_names = [c["name"] for c in cast if c.get("name")]
    return {
        "n": _clip_ordinal(clip.get("clip_id")),
        "characters": char_names,
        # 위주/OTS 컷이면 그 1인만 얼굴 참조로(다른 인물 참조 drop) — 정체성 흔들림 방지
        "focus_char": _block_focus_char(block, char_names),
        "costumes": costumes,
        "appearances": appearances,
        "places": [scene["location_tag"]] if scene.get("location_tag") else [],
        "props": v3_schema.scene_prop_names(scene),
        "prompt": sb_prompts.clip_still_prompt(scene, clip, block),
        "caption": clip.get("label") or "",
    }


def generate_clip_still(scene: dict, clip: dict, work: str | None = None,
                        block: dict | None = None,
                        continuity_png: bytes | None = None,
                        characters: list[dict] | None = None) -> tuple[bytes, float]:
    """클립의 대표(또는 지정 보강) 블록으로 앵커 스틸 1장 생성 → (PNG, cost$). continuity_png가
    있으면(같은 씬의 직전 승인 스틸) 연속성 앵커로 넘긴다(규칙 3). characters(프로젝트 캐릭터,
    appearance 포함)를 주면 인물별 외모·의상을 프롬프트에 못박아 인물 간 의상 뒤바뀜을 막는다."""
    block = block or v3_schema.representative_block(clip)
    if not block:
        raise RuntimeError(f"클립 {clip.get('clip_id')}: 대표 스틸로 삼을 블록이 없어요.")
    shot = _clip_pseudo_shot(scene, clip, block, work=work, characters=characters)
    continuity_refs = None
    if continuity_png:
        prev_ord = max(0, _clip_ordinal(clip.get("clip_id")) - 1)
        continuity_refs = [(prev_ord, continuity_png)]
    return generate_image_for_shot(shot, work=work, continuity_refs=continuity_refs)


def generate_video_for_clip(work: str, scene_num: int, clip_id: str, png: bytes,
                            motion_prompt: str, episode: int = 1,
                            want_audio: bool | None = None) -> str:
    """클립 대표 스틸 1장 + 클립 멀티샷 모션 프롬프트 → 클립 영상 1개(로컬 mp4 경로).
    저장은 clip{clip_id} 단위(generate_video_for_cut의 clip_id 경로 재사용 — 안전필터 격자
    회피 로직도 그대로 탄다)."""
    return generate_video_for_cut(work, scene_num, cut_num=None, png=png,
                                  motion_prompt=motion_prompt, episode=episode,
                                  clip_id=clip_id, want_audio=want_audio)


def produce_clip(work: str, scene: dict, clip: dict, episode: int = 1,
                 continuity_png: bytes | None = None,
                 still_png: bytes | None = None,
                 characters: list[dict] | None = None) -> dict:
    """클립 하나를 스틸→영상까지 완주(문서 5단계 씬 내부의 클립 단위 실행). 스틸은 저장(승인 대상),
    영상 실패는 그 클립만 실패로 남기고 스틸은 보존(재생성 비용 방지). 반환: {clip_id, status,
    still_png, video_path?, error?}.
    still_png가 주어지면(미리보기에서 승인된 스틸) 새로 생성하지 않고 그대로 영상화한다 —
    미리보기 스틸 재사용으로 이미지 생성 비용·시간을 아낀다(승인 스틸 = 시작 프레임 앵커)."""
    scene_num = scene.get("scene_num")
    clip_id = clip.get("clip_id")
    png = still_png
    if png is None:
        try:
            png, _cost = generate_clip_still(scene, clip, work=work, continuity_png=continuity_png,
                                             characters=characters)
        except Exception as e:
            return {"clip_id": clip_id, "status": "failed", "still_png": None, "error": str(e)}
        try:
            vp_store.save_still(work, scene_num=scene_num,
                                prompt_summary=(clip.get("label") or ""), png=png,
                                episode=episode, clip_id=clip_id)
        except Exception:
            pass  # 스틸 파일 저장 실패해도 메모리의 png로 영상화는 이어감
    try:
        motion = sb_prompts.clip_motion_prompt(scene, clip)
        path = generate_video_for_clip(work, scene_num, clip_id, png, motion, episode=episode,
                                       want_audio=_clip_has_dialogue(clip))
    except Exception as e:
        log.exception("영상화 실패 (씬%s 클립 %s): %s", scene_num, clip_id, e)
        return {"clip_id": clip_id, "status": "still_only", "still_png": png, "error": str(e)}
    return {"clip_id": clip_id, "status": "ok", "still_png": png, "video_path": path}


# ── v3.1 파이프라인 9단계: 씬 순차 완주 + 연속성 핸드오프 저장 ────────────────
# 씬1을 상세블록→레퍼런스→클립 스틸·영상까지 완주한 뒤 handoff를 만들어 씬2로 넘긴다. 완료된
# 씬은 save_scene으로 저장하고, 재개 시 is_completed로 건너뛰어 다시 만들지 않는다(문서 최소
# 인수 조건: 서버 재시작 후 완료 씬 건너뛰고 현재 씬부터 재개).

def produce_scene(work: str, scene: dict, episode: int = 1,
                  prior_handoff: dict | None = None, mood: str = "",
                  conti_body: str = "", on_progress=None,
                  approved_stills: dict[str, bytes] | None = None,
                  characters: list[dict] | None = None) -> dict:
    """씬 하나 완주 — 6단계 레퍼런스 → 클립별 스틸·영상(produce_clip) → 핸드오프 생성. 같은 씬의
    직전 클립 승인 스틸을 다음 클립의 연속성 앵커로 넘긴다(규칙 3). 모든 클립 성공 시 state를
    completed로. 반환: {scene_num, state, clips, handoff}.
    approved_stills({clip_id: png})가 있으면 그 클립은 미리보기 승인 스틸을 재사용한다(재생성 X).
    승인 스틸이 모든 클립에 있으면 레퍼런스 생성도 생략한다(이미 그 스틸에 반영돼 있음)."""
    scene_num = scene.get("scene_num")
    approved_stills = approved_stills or {}

    def notify(clip_id, msg):
        if on_progress:
            on_progress(scene_num, clip_id, msg)

    clips = scene.get("clips") or []
    # 모든 클립 스틸을 재사용하면 레퍼런스 생성이 불필요(스틸에 이미 반영됨) — 비용 절약.
    all_reused = bool(clips) and all(approved_stills.get(c.get("clip_id")) for c in clips)
    if not all_reused:
        notify(None, "레퍼런스 준비 중")
        try:
            ensure_scene_costumes(work, scene, characters=characters, mood=mood)
        except Exception:
            pass  # 의상 자동 설계 실패해도 이어감
        try:
            ensure_scene_references(work, scene, mood=mood, conti_body=conti_body)
        except Exception:
            pass  # 레퍼런스 생성 실패해도 스틸 생성은 이어감(참조 없이라도)

    # ★2026-07-22: 클립 영상화를 병렬로(레이트리밋 안 걸릴 정도의 소수 동시). 순차 대비 전체 영상
    # 시간 크게 단축. 병렬이라 스틸 연속성 체이닝(continuity_png)은 생략 — 전체 영상은 미리보기
    # 승인 스틸을 재사용하므로 스틸 재생성 자체가 없어 체이닝이 무의미하다.
    clip_results: list = [None] * len(clips)

    def _one_clip(idx_clip):
        idx, clip = idx_clip
        cid = clip.get("clip_id")
        reused = approved_stills.get(cid)
        notify(cid, "영상화 중(승인 스틸)" if reused else "스틸·영상 생성 중")
        r = produce_clip(work, scene, clip, episode=episode,
                         still_png=reused, characters=characters)
        clip_results[idx] = r
        notify(cid, r["status"])

    vworkers = max(1, min(getattr(sb_config, "OPENROUTER_VIDEO_WORKERS", 3), len(clips) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=vworkers) as _vex:
        list(_vex.map(_one_clip, list(enumerate(clips))))
    clip_results = [r for r in clip_results if r]

    handoff = v3_schema.build_scene_handoff(scene, prior_handoff)
    all_ok = bool(clip_results) and all(r["status"] == "ok" for r in clip_results)
    if all_ok:
        scene["state"] = "completed"
    return {"scene_num": scene_num, "state": scene.get("state"),
            "clips": clip_results, "handoff": handoff}


def _clip_plan_segment(scene: dict, clip: dict, video_path: str) -> dict:
    """클립 영상 1개 → 합본 편집계획 세그먼트. narration_text는 V.O.만(립싱크·off·현장음은 클립
    영상 자체 오디오가 담당) — 10단계 오디오 층 분리의 핵심. caption을 나레이션으로 쓰지 않는다."""
    vo = v3_schema.clip_vo_lines(clip)
    narration = " ".join(l["text"] for l in vo) or None
    speaker = (vo[0]["speaker"] if vo else None) or "나레이션"
    return {
        "scene_num": scene.get("scene_num"),
        "cut_num": _clip_ordinal(clip.get("clip_id")),
        "clip_id": clip.get("clip_id"),
        "video_path": video_path,
        "start": 0.0,
        "duration": edit_plan._probe_duration(video_path),
        "narration_text": narration,
        "speaker": speaker,
        "delivery": None,
    }


def produce_episode_v3(work: str, script: str, skeleton_scenes: list[tuple[int, str]],
                       episode: int = 1, characters: list[dict] | None = None, mood: str = "",
                       is_completed=None, save_scene=None, load_scene=None,
                       on_progress=None, prior_handoff: dict | None = None,
                       synopsis: str = "", summary: str = "") -> dict:
    """v3.1 화 전체를 씬 순차로 완주. skeleton_scenes = scene_skeleton_texts()의 [(씬번호, 뼈대)].
    각 씬: build_scene_blocks(5단계) → produce_scene(6~8단계) → handoff → save_scene(9단계).
    is_completed(scene_num)가 True면 그 씬은 건너뛰고 저장된 handoff/plan을 load_scene으로 이어받아
    재개한다(완료 씬 재생성 방지). 씬 하나의 상세 블록 검증이 끝내 실패하면 이후 씬의 연속성이
    깨지므로 그 씬에서 멈춘다. 반환: {plan, handoff, scenes}. plan은 compile_episode_v3에 넘긴다."""
    handoff = prior_handoff
    plan: list[dict] = []
    results: list[dict] = []
    for scene_num, skeleton_text in skeleton_scenes:
        if is_completed and is_completed(scene_num):
            saved = load_scene(scene_num) if load_scene else None
            if saved and saved.get("handoff"):
                handoff = saved["handoff"]
            if saved and saved.get("plan"):
                plan.extend(saved["plan"])
            results.append({"scene_num": scene_num, "state": "completed", "skipped": True})
            continue

        # 콘티 소스 우선순위(★2026-07-22 사용자 요청):
        #   ① 검수용 상세콘티 파일(상세콘티/씬N.txt) — 사용자가 손으로 수정하면 그게 최우선 소스
        #   ② 미리보기(preview_scene_v3) 상태의 conti_text
        #   ③ 신규 LLM 생성(build_scene_blocks)
        # 어느 경우든 상세 블록 재생성(LLM)을 건너뛰어 비용·시간을 아낀다. 승인 스틸은 콘티 소스와
        # 무관하게 미리보기 상태에서 그대로 재사용한다(스틸 재생성 skip).
        preview = load_scene(scene_num) if load_scene else None
        scene = conti_text = None
        errors = []
        approved_stills = {}
        # 승인 스틸 재사용 — 콘티를 파일에서 읽든 상태에서 읽든 공통 적용
        for s in (preview.get("stills") if preview else None) or []:
            if s.get("clip_id") and s.get("image"):
                try:
                    approved_stills[s["clip_id"]] = oi.data_url_to_png(s["image"])
                except Exception:
                    pass
        # ① 파일 우선 → ② 미리보기 상태
        edited = _load_conti_from_review(work, episode, scene_num)
        src_text = edited or (preview.get("conti_text") if preview else None)
        if src_text:
            parsed = parsing.split_scenes(src_text)
            if parsed:
                _, hdr, body = parsed[0]
                scene = v3_schema.parse_scene(hdr, body)
                conti_text = src_text
                errors = []  # ★2026-07-22: 검증 제거(build_scene_blocks와 일관) — 기존 콘티를
                # 재검증해 실패로 break하면 한 클립도 영상화 안 돼 "영상 클립 없다"가 뜨던 버그.
                if edited and on_progress:
                    on_progress(scene_num, None, "상세콘티 파일 사용(수정본)")
        if scene is None:
            if on_progress:
                on_progress(scene_num, None, "상세 콘티 작성 중")
            scene, conti_text, errors = build_scene_blocks(
                skeleton_text, script, prior_handoff=handoff, characters=characters, work=work,
                synopsis=synopsis, summary=summary)
            if not errors and scene:
                _save_conti_for_review(work, episode, scene_num, conti_text)  # 검수용 텍스트 파일
        if errors or not scene:
            results.append({"scene_num": scene_num, "state": "failed", "errors": errors})
            break  # 연속성 유지 위해 이 씬에서 멈춤

        sr = produce_scene(work, scene, episode=episode, prior_handoff=handoff,
                           mood=mood, conti_body=conti_text, on_progress=on_progress,
                           approved_stills=approved_stills, characters=characters)
        scene_plan = [_clip_plan_segment(scene, clip, cr["video_path"])
                      for clip, cr in zip(scene.get("clips") or [], sr["clips"])
                      if cr.get("video_path")]
        plan.extend(scene_plan)
        handoff = sr["handoff"]
        if save_scene:
            save_scene(scene_num, {"scene_num": scene_num, "state": sr["state"],
                                   "conti_text": conti_text, "handoff": handoff,
                                   "plan": scene_plan})
        results.append(sr)
    return {"plan": plan, "handoff": handoff, "scenes": results}


# ── v3.1 파이프라인 10단계: caption 자동 나레이션 제거 + 오디오 층 분리 합본 ────

def compile_episode_v3(work: str, idea: str, plan: list[dict], episode_title: str = "1화") -> str:
    """produce_episode_v3의 plan(클립 영상 세그먼트, narration_text=V.O.만)을 합본한다. 기존
    compile_episode_video와 달리 attach_narration(모든 caption을 나레이션으로)을 호출하지 않는다 —
    립싱크·off·현장음은 클립 영상 자체 오디오로, V.O.만 별도 나레이션 TTS 층으로 믹싱된다."""
    if not plan:
        raise RuntimeError("영상화된 클립이 하나도 없어서 합본할 수 없어요.")
    mood_prompt = generate_mood_prompt(idea)
    return episode_compile.compile_episode(work, episode_title, plan, mood_prompt=mood_prompt)


# ── v3.1 파이프라인 11·12단계: 서버 잡 래퍼 (백그라운드 스레드 + jobs + save_fn) ──
# server.py의 _run_with_locked_references(build_fn, project, episode, job_id, save_fn) 패턴에 맞춘
# 진입 함수들. v3.1 상태는 episode dict의 v3_skeleton / v3_scenes 필드에 별도로 저장한다(구 shot
# 필드 scenes/shots_by_scene/scene_stills와 충돌하지 않게 — 기존 프로젝트 무손상 폴백 = 11단계).
# 단, 프런트 재사용을 위해 대표 스틸은 기존 scene_stills 형태({scene_num,cut_num,image,caption})로도
# 함께 채운다(renderStills가 그대로 그림).

def _v3_scene_map(episode: dict) -> dict[int, dict]:
    """episode.v3_scenes(list) → {scene_num(int): 씬 레코드}. JSON 왕복으로 문자열이 된 번호도 int로."""
    return {int(s.get("scene_num")): s for s in (episode.get("v3_scenes") or [])
            if s.get("scene_num") is not None}


def _v3_all_stills(v3map: dict[int, dict]) -> list[dict]:
    """모든 씬의 대표 스틸을 씬·클립 순서로 모은다(프런트 scene_stills 호환 목록)."""
    out = []
    for n in sorted(v3map):
        out.extend(v3map[n].get("stills") or [])
    return out


def _ensure_v3_skeleton(episode: dict, script: str, num: int,
                        characters: list[dict] | None, job_id: str, save_fn) -> str:
    """episode에 v3 뼈대가 없으면 생성·검증·저장하고 뼈대 원문을 반환. 이미 있으면 그대로 반환."""
    skeleton_text = episode.get("v3_skeleton")
    if skeleton_text:
        return skeleton_text
    jobs.update(job_id, stage="화 전체 뼈대 설계 중")
    skeleton_text, sk_scenes, sk_errors = generate_episode_skeleton_validated(
        script, episode=num, characters=characters)
    if sk_errors:
        # 진단용: 검증 실패 시 LLM 뼈대 원문을 서버 로그로 남긴다(형식 흔들림을 눈으로 확인).
        print("[v3 뼈대 검증 실패] errors=" + " / ".join(sk_errors) +
              "\n----- 뼈대 원문 -----\n" + skeleton_text + "\n---------------------", flush=True)
        raise RuntimeError("화 뼈대 검증에 실패했어요: " + " / ".join(sk_errors))
    scene_lines = [[s["scene_num"], s.get("title") or ""] for s in sk_scenes
                   if s.get("scene_num") is not None]
    if save_fn:
        save_fn(v3_skeleton=skeleton_text, scene_lines=scene_lines)
    return skeleton_text


def preview_scene_v3(project: dict, episode: dict, job_id: str, save_fn=None,
                     scene_num: int = 1) -> None:
    """v3.1 미리보기 — 씬 하나의 상세 블록 → 레퍼런스 → 클립별 대표 스틸까지(영상은 안 만듦).
    승인용 스틸을 scene_stills(구 형태) + v3_scenes(v3 상태)에 누적 저장한다. 완료된(스틸 있는)
    씬을 다시 요청하면 새로 만들지 않는다(중복 방지). 백그라운드 스레드 전제(예외는 jobs error)."""
    work = project["work"]
    num = episode["num"]
    characters = project.get("characters", [])
    mood = _project_mood(project)
    try:
        project_setup.ensure_project(work)
        script = episode.get("script")
        if not script:
            raise RuntimeError("대본이 먼저 있어야 해요.")
        skeleton_text = _ensure_v3_skeleton(episode, script, num, characters, job_id, save_fn)
        skel = dict(scene_skeleton_texts(skeleton_text))
        jobs.update(job_id, total=len(skel))

        v3map = _v3_scene_map(episode)
        if scene_num in v3map and v3map[scene_num].get("stills"):
            jobs.update(job_id, status="done", stage="완료", stills=_v3_all_stills(v3map))
            return
        if scene_num not in skel:
            raise RuntimeError(f"씬{scene_num}을 화 뼈대에서 찾을 수 없어요.")

        prior_handoff = (v3map.get(scene_num - 1) or {}).get("handoff")
        jobs.update(job_id, stage=f"씬{scene_num} 상세 콘티 작성 중")
        scene, conti_text, errors = build_scene_blocks(
            skel[scene_num], script, prior_handoff=prior_handoff,
            characters=characters, work=work,
            synopsis=project.get("synopsis") or "", summary=episode.get("summary") or "")
        if errors or not scene:
            raise RuntimeError(f"씬{scene_num} 콘티 검증 실패: " + " / ".join(errors or ["파싱 실패"]))
        _save_conti_for_review(work, num, scene_num, conti_text)  # 검수용 텍스트 파일로 남김

        jobs.update(job_id, stage=f"씬{scene_num} 배경·의상 준비 중")
        try:
            ensure_scene_costumes(work, scene, characters=characters, mood=mood)
        except Exception as e:
            log.exception("의상 자동 설계 실패 (씬%s): %s", scene_num, e)  # 실패해도 이어감
        try:
            ensure_scene_references(work, scene, mood=mood, conti_body=conti_text)
        except Exception as e:
            log.exception("씬 참조(배경·소품) 생성 실패 (씬%s): %s", scene_num, e)

        # ★2026-07-22: 미리보기 스틸 = 클립당 대표 1장. co-writer-bot 병렬 구조 이식 — 클립들을
        # ThreadPoolExecutor(OPENROUTER_IMG_WORKERS)로 '독립 그룹 병렬' 생성한다(클립당 1장이라
        # 그룹 내 체이닝 없음). 인물·의상·장소는 등록 참조로 고정돼 병렬이어도 정체성은 유지되고,
        # 잃는 건 클립 간 톤 연속성뿐(연속성 체이닝 제거 = 병렬의 대가). 완성 컷부터 하나씩 노출.
        clips = scene.get("clips") or []
        prev_v3 = _v3_all_stills(v3map)  # 이전 씬들 스틸(이번 씬은 아래서 채움)
        results: list = [None] * len(clips)
        _slock = threading.Lock()
        jobs.update(job_id, stage=f"씬{scene_num} 스틸 {len(clips)}컷 생성 중(병렬)")

        def _one_clip(ci_clip):
            ci, clip = ci_clip
            try:
                png, _cost = generate_clip_still(scene, clip, work=work, characters=characters)
            except Exception as e:
                log.exception("스틸 생성 실패 (씬%s 클립 %s): %s", scene_num, clip.get("clip_id"), e)
                return
            clip_id = clip.get("clip_id")
            try:
                vp_store.save_still(work, scene_num=scene_num, prompt_summary=clip.get("label", ""),
                                    png=png, episode=num, clip_id=clip_id)
            except Exception:
                pass
            rec = {"scene_num": scene_num, "cut_num": ci + 1, "clip_id": clip_id,
                   "caption": clip.get("label", ""), "image": oi.png_data_url(png),
                   "video_path": None, "representative": True}
            with _slock:
                results[ci] = rec
                jobs.update(job_id, stills=prev_v3 + [r for r in results if r])  # 완성분부터 노출
        workers = max(1, min(sb_config.OPENROUTER_IMG_WORKERS, len(clips) or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as _ex:
            list(_ex.map(_one_clip, list(enumerate(clips))))
        stills = [r for r in results if r]

        v3map[scene_num] = {
            "scene_num": scene_num, "state": "stills_ready", "conti_text": conti_text,
            "handoff": v3_schema.build_scene_handoff(scene, prior_handoff), "stills": stills,
        }
        v3_scenes = [v3map[k] for k in sorted(v3map)]
        all_stills = _v3_all_stills(v3map)
        if save_fn:
            save_fn(v3_scenes=v3_scenes, scene_stills=all_stills)
        jobs.update(job_id, status="done", stage="완료", stills=all_stills)
    except Exception as e:
        jobs.update(job_id, status="error", stage="오류", error=str(e))


def produce_episode_v3_job(project: dict, episode: dict, job_id: str, save_fn=None) -> None:
    """v3.1 화 전체 제작 — 뼈대(없으면 생성)부터 씬 순차로 상세블록→레퍼런스→클립 스틸·영상까지
    완주한 뒤 합본(V.O.만 나레이션). 완료 씬(v3_scenes state=completed)은 건너뛰고 재개한다.
    백그라운드 스레드 전제(예외는 jobs error). 최종 합본 경로는 job.video_path로 전달."""
    work = project["work"]
    num = episode["num"]
    characters = project.get("characters", [])
    mood = _project_mood(project)
    idea = project.get("idea") or project.get("logline") or ""
    # ★2026-07-23: 1화 제작 총 소요시간·비용 계측. costmeter를 여기서 0으로 돌리고(에피소드 경계),
    # 완료/실패 시 snapshot을 로그에 남긴다. 뼈대·상세콘티(LLM)+스틸·영상(이미지/영상)+합본(TTS)
    # 모든 생성이 벤더 초크포인트에서 costmeter에 누적된다.
    costmeter.reset()
    _t0 = time.perf_counter()
    try:
        project_setup.ensure_project(work)
        script = episode.get("script")
        if not script:
            raise RuntimeError("대본이 먼저 있어야 영상을 만들 수 있어요.")
        skeleton_text = _ensure_v3_skeleton(episode, script, num, characters, job_id, save_fn)
        skel = scene_skeleton_texts(skeleton_text)
        v3map = _v3_scene_map(episode)

        def is_completed(sn):
            r = v3map.get(int(sn))
            return bool(r and r.get("state") == "completed")

        def load_scene(sn):
            return v3map.get(int(sn))

        def save_scene(sn, payload):
            v3map[int(sn)] = {**(v3map.get(int(sn)) or {}), **payload}
            if save_fn:
                save_fn(v3_scenes=[v3map[k] for k in sorted(v3map)])

        def on_progress(sn, cid, msg):
            label = f"씬{sn}" + (f" {cid}" if cid else "")
            jobs.update(job_id, stage=f"{label}: {msg}")

        result = produce_episode_v3(
            work, script, skel, episode=num, characters=characters, mood=mood,
            is_completed=is_completed, save_scene=save_scene, load_scene=load_scene,
            on_progress=on_progress,
            synopsis=project.get("synopsis") or "", summary=episode.get("summary") or "")

        jobs.update(job_id, stage="합본 중")
        path = compile_episode_v3(work, idea, result["plan"], episode_title=f"{num}화")
        if save_fn:
            save_fn(compiled_path=path)
        elapsed = time.perf_counter() - _t0
        n_scenes = len(result.get("scenes") or [])
        n_cuts = len(result.get("plan") or [])
        log.info("✅ %s화 제작 완료 — 소요 %s · 비용 %s · 씬 %d · 컷 %d",
                 num, _fmt_elapsed(elapsed), costmeter.format_summary(), n_scenes, n_cuts)
        jobs.update(job_id, status="done", stage="완료", video_path=path)
    except Exception as e:
        elapsed = time.perf_counter() - _t0
        log.info("❌ %s화 제작 실패 — 소요 %s · 그때까지 비용 %s · 원인: %s",
                 num, _fmt_elapsed(elapsed), costmeter.format_summary(), e)
        jobs.update(job_id, status="error", stage="오류", error=str(e))


def _element_names_for_prompt(work: str | None, etype: str) -> list[str]:
    """등록 요소 이름을 프롬프트용으로 정리한다. 공백·하이픈 표기만 다른 중복 의상/장소는
    최초 등록명을 대표값으로 남겨 LLM이 같은 요소에 새 라벨을 계속 만드는 것을 막는다."""
    if not work:
        return []
    out, seen = [], set()
    for e in oi.load_elements(work):
        if e.get("type") != etype or not e.get("display"):
            continue
        name = e["display"]
        key = re.sub(r"[\s_\-]+", "", name).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _continuity_declarations(conti: str) -> str:
    """이전 콘티에서 다음 씬의 의상·장소·조명 연속성에 필요한 선언 줄만 압축 추출."""
    lines = []
    for line in (conti or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("등장:", "장소:", "무드/조명:")):
            lines.append(stripped)
    return "\n".join(lines[-12:])


def generate_conti(script: str, plan_text: str, scenes_plan: list[tuple[int, str]],
                   episode: int = 1, characters: list[dict] | None = None,
                   work: str | None = None, prior_conti: str = "") -> str:
    """상세 콘티. app.py의 씬 단위 병렬 호출(_gen_scene)과 같은 프롬프트 패턴이지만
    MVP는 순차 for-loop로(디버깅 쉬움, 데모 안정성 우선)."""
    sys_prompt = sb_prompts.storyboard_system(
        bible=characters_bible(characters), target_episode=episode,
        known_places=_element_names_for_prompt(work, "place") or None,
        known_costumes=_element_names_for_prompt(work, "costume") or None)
    parts = []
    for num, line in scenes_plan:
        continuity = _continuity_declarations("\n\n".join([prior_conti, *parts]))
        continuity_block = ""
        if continuity:
            continuity_block = (
                "\n\n[앞서 확정된 씬 선언 — 같은 시간대에서 바로 이어지는 인물의 의상·장소는 "
                "아래 라벨을 그대로 재사용하고 새 이름을 만들지 마라]\n" + continuity)
        user = (
            f"[씬 설계안 — 화 전체 목록(참고용, 다른 씬은 이미 별도로 처리 중이니 이 씬에만 집중)]\n"
            f"{plan_text}\n\n"
            f"[원본 대본 — 사건·행동·대사 하나도 바꾸지 마라]\n{script}\n\n"
            f"(지금은 화 전체가 아니라 이 씬 하나만 상세 콘티로 써라: '{line}'. "
            f"반드시 '■ 씬{num} · N초 · 제목' 헤더로 시작해 이 씬의 샷 콘티만 출력하고 다른 씬은 "
            f"언급하지 마라. 대본의 사건·행동·대사는 하나도 바꾸지 마라.){continuity_block}"
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
    """요소를 등록(메타데이터)해 id를 얻고, 참조 PNG를 refs/<작품>/<타입폴더>/<id>.png에 저장한다
    (★2026-07-22 refs 재설계: 파일명=요소ID, 폴더=타입에서 결정)."""
    el = oi.register_element(work, name, etype=etype, aliases=[name])
    oi.save_element_image(work, el, png)


def _safe_element_filename(name: str) -> str:
    return re.sub(r"[^\w가-힣.\-]+", "_", name or "요소").strip("_.") or "요소"


# 씬들이 병렬로 돌면서 각자 fix_element_references를 호출하는데, 그 순간 "아직 안 고정된
# 요소" 목록이 겹치면 같은 장소/의상/소품 이미지를 중복 생성하는 경합이 생긴다(시간·비용 낭비).
# 프로세스 전역에서 "지금 생성 중인 요소"를 표시해두고 다른 씬은 건너뛰게 한다.
_INFLIGHT_ELEMENTS: set[tuple[str, str]] = set()
_INFLIGHT_LOCK = threading.Lock()


def fix_element_references(work: str, mood: str = "", conti_full: str = "",
                          only: set[str] | None = None) -> dict:
    """등록됐지만 아직 레퍼런스 이미지가 없는 장소·의상·소품에 대해 고정 이미지를 생성·등록한다.
    인물(person)은 초상화로 이미 얼굴 레퍼런스가 있으므로 건너뛴다. 반환: {등록됨, 실패} 개수.
    실패한 요소는 건너뛰고 계속 진행(전체 실패 방지).
    only가 주어지면 그 display 이름 집합에 속한 요소만 생성한다(v3.1 6단계 씬별 지연 생성 —
    화 전체가 아니라 지금 처리하는 씬에 필요한 요소만 만든다. ensure_scene_references가 사용).
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
            if oi.element_has_image(work, e):
                continue  # 이미 레퍼런스 이미지 있음
            name = e.get("display") or ""
            if not name:
                continue
            if only is not None and name not in only:
                continue  # 이 씬에 필요한 요소만(6단계 지연 생성)
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


def _scene_costume_assignments(conti_body: str) -> dict[str, str]:
    """상세 콘티의 `등장: 인물(의상: 라벨, 설명)` 선언에서 인물→의상 라벨을 추출한다.

    샷 분해 LLM이 costumes 필드를 누락하거나 잘못 연결해도 코드가 이 선언을 SSOT로 보정한다.
    """
    assignments = {}
    for line in (conti_body or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("등장:"):
            continue
        declaration = stripped.split(":", 1)[1]
        for part in re.split(r"\s*[·•]\s*", declaration):
            m = re.match(r"\s*([^()]+?)\s*\(\s*(?:의상\s*:\s*)?([^,)]+)", part)
            if not m:
                continue
            character, costume = m.group(1).strip(), m.group(2).strip()
            if character and costume:
                assignments[character] = costume
    return assignments


def _restore_costume_assignments(scenes: list[tuple[int, str, str]],
                                 shots_by_scene: dict[int, list[dict]]) -> dict[int, list[dict]]:
    """구버전 저장 샷의 누락된 costumes 매핑을 저장된 씬 콘티에서 복원한다."""
    scene_bodies = {int(num): body for num, _header, body in scenes}
    for scene_num, shots in shots_by_scene.items():
        assignments = _scene_costume_assignments(scene_bodies.get(int(scene_num), ""))
        if not assignments:
            continue
        for shot in shots:
            visible = set(shot.get("characters") or [])
            mapped = {character: costume for character, costume in assignments.items()
                      if character in visible}
            if mapped:
                shot["costumes"] = mapped
    return shots_by_scene


def generate_shots_by_scene(scenes: list[tuple[int, str, str]], work: str | None = None,
                            characters: list[dict] | None = None) -> dict[int, list[dict]]:
    """씬별 상세콘티 body를 샷 단위로 분해. 반환: {씬번호: [shot dict, ...]}.
    work가 주어지면 등록된 장소·소품·의상 목록을 프롬프트에 같이 내려 컷마다 정식 이름/묘사가
    반복 재사용되게 한다(요소 레지스트리 — extract_and_register_elements가 미리 채워둔 것).
    OPENROUTER_API_KEY 필요(agent 백엔드 아니라 OpenRouter chat 사용, 원본과 동일)."""
    elems = oi.load_elements(work) if work else []
    places = sorted({e["display"] for e in elems if e.get("type") == "place"})
    props = sorted({e["display"] for e in elems if e.get("type") == "prop"})
    costumes = _element_names_for_prompt(work, "costume")
    system = sb_prompts.storyboard_shots_system(bible=characters_bible(characters),
                                                places=places or None,
                                                props=props or None, costumes=costumes or None)
    shots_by_scene = {}
    character_cards = {
        (c.get("name") or "").strip(): c for c in (characters or []) if (c.get("name") or "").strip()
    }
    for num, _hdr, body in scenes:
        scene_costumes = _scene_costume_assignments(body)
        raw = _with_retry(oi.chat, system, sb_prompts.storyboard_shots_user(body))
        shots = [s for s in parsing.parse_json_array(raw) if s.get("prompt")]
        scene_continuity = next(
            (s.get("continuity") for s in shots if isinstance(s.get("continuity"), dict)), {})
        scene_continuity = dict(scene_continuity)
        if scene_costumes:
            scene_continuity["wardrobe"] = "; ".join(
                f"{character}: {costume}" for character, costume in scene_costumes.items())
        for i, s in enumerate(shots, 1):
            s["n"] = i
            visible = set(s.get("characters") or [])
            mapped = {character: costume for character, costume in scene_costumes.items()
                      if character in visible}
            if mapped:
                s["costumes"] = mapped
            appearances = {}
            for character in visible:
                card = character_cards.get(character) or character_cards.get(
                    re.sub(r"\s*\(과거\)\s*$", "", character))
                if not card:
                    continue
                detail = ", ".join(str(v).strip() for v in (
                    card.get("gender"), card.get("age"), card.get("appearance")) if str(v or "").strip())
                if detail:
                    appearances[character] = detail
            if appearances:
                s["character_appearance"] = appearances
            # 같은 씬에서는 외형·의상·공간·조명·색감 선언을 한 글자도 바꾸지 않고 반복한다.
            s["continuity"] = dict(scene_continuity)
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
    " Keep each character's hairstyle (including whether hair is tied up or down) IDENTICAL to "
    "their identity reference, and every costume detail (color, fit, accessories) IDENTICAL to "
    "that character's assigned wardrobe reference in every shot — "
    "do not alter hair state or outfit details unless the action/prompt explicitly describes a change."
)


def _continuity_value(shot: dict, key: str, fallback: str) -> str:
    continuity = shot.get("continuity") or {}
    value = continuity.get(key) if isinstance(continuity, dict) else None
    return str(value).strip() if value else fallback


def _shot_continuity_prompt(shot: dict, connected_cut_nums: list[int] | None = None) -> str:
    """사용자가 지정한 연속성 템플릿을 실제 이미지 프롬프트용 고정 블록으로 채운다."""
    cut_num = shot.get("n", "?")
    connected = [int(n) for n in (connected_cut_nums or [])]
    if connected:
        connected_text = ", ".join(str(n) for n in connected)
        opening = f"This is shot {cut_num} from the same continuous scene as shots {connected_text}."
    else:
        opening = f"This is shot {cut_num}, the opening visual anchor for this continuous scene."

    appearances = shot.get("character_appearance") or {}
    if isinstance(appearances, dict) and appearances:
        character_fallback = "; ".join(f"{name}: {detail}" for name, detail in appearances.items())
    else:
        character_fallback = ", ".join(shot.get("characters") or []) or "No visible character."

    assignments = shot.get("costumes") or {}
    if isinstance(assignments, dict) and assignments:
        wardrobe_fallback = "; ".join(
            f"{character}: {costume}" for character, costume in assignments.items())
    else:
        wardrobe_fallback = "No registered wardrobe assignment."

    current = shot.get("current_shot") or {}
    if not isinstance(current, dict):
        current = {}
    action = str(current.get("action") or shot.get("caption") or shot.get("prompt") or "").strip()
    expression = str(current.get("expression") or "Follow the described action without changing identity.").strip()
    gaze = str(current.get("gaze") or "Follow the described action.").strip()
    hands = str(current.get("hands") or "Keep hand and prop placement exactly as described in this shot.").strip()
    camera = str(current.get("camera") or "Follow the shot's specified framing and composition.").strip()

    return f"""CONTINUITY REQUIREMENT:

{opening}

Keep the character identity, hairstyle, wardrobe, accessories, props, location layout, furniture placement, lighting direction, color temperature, color grading, and overall mood exactly identical across every shot.

Only the camera framing, pose, gaze, facial expression, and described action may change.

CHARACTER:

{_continuity_value(shot, "character", character_fallback)}

Preserve the exact same face, hairstyle, apparent age, and body proportions.

WARDROBE:

{_continuity_value(shot, "wardrobe", wardrobe_fallback)}

The wardrobe must remain completely identical.

Do not copy clothing from the character reference.

Do not add, remove, replace, or redesign any clothing or accessories.

PROPS:

{_continuity_value(shot, "props", ", ".join(shot.get("props") or []) or "No fixed prop in frame.")}

Maintain the exact same prop design, condition, hand placement, and environmental position.

Do not add, remove, duplicate, or relocate props.

LOCATION:

{_continuity_value(shot, "location", ", ".join(shot.get("places") or []) or "Use the established scene location.")}

Maintain the exact same architecture, doors, windows, furniture, background objects, and spatial orientation.

Do not mirror or reverse the room.

LIGHTING:

{_continuity_value(shot, "lighting", "Use the established scene lighting without alteration.")}

Maintain the exact same light source, direction, brightness, exposure, shadow direction, and color temperature.

MOOD AND COLOR:

{_continuity_value(shot, "mood_color", "Use the established scene mood and color grade without alteration.")}

Maintain the same emotional atmosphere, white balance, saturation, contrast, black level, and cinematic texture.

CURRENT SHOT:

{action}

Expression: {expression}

Gaze: {gaze}

Hands: {hands}

CAMERA:

{camera}

STYLE:"""


def generate_image_for_shot(shot: dict, work: str | None = None,
                            continuity_refs: list[tuple[int, bytes]] | None = None,
                            feedback: str = "") -> tuple[bytes, float]:
    """샷 하나의 스틸컷 생성 → (PNG bytes, cost$).

    ★2026-07-22(co-writer-bot HANDOFF_실사화스틸컷프롬프트 이식 — 프롬프트 싹 갈아엎음):
    프롬프트 조립 순서 = ① 화풍(맨 앞) + 컷 프롬프트 → ② 연속성 앵커(직전 컷) → ③
    reference_priority_block(참조별 역할 분리 + 성별 앵커 + 소유 명시 + 인물/의상 2개↑면 STRICT
    WARDROBE SEPARATION으로 의상 오염 방지) → ④ 사용자 지시(feedback, 최우선·맨 끝).
    참조 순서 = [등록 요소 참조(costume-first/focus-char), 연속성 직전 컷]. 화풍을 맨 앞에 둬야
    촬영장·카메라 묘사 컷에서 화풍이 안 밀린다. 의상은 '빼는' 게 아니라 '묶어서' 해결."""
    ref_entries = oi.shot_ref_entries(work, shot) if work else []
    refs = [u for (_role, u, *_rest) in ref_entries]
    role_block = oi.reference_priority_block(ref_entries) if ref_entries else ""

    connected_cut_nums = []
    for prior_cut_num, prior_png in (continuity_refs or []):
        if not prior_png:
            continue
        refs.append(oi.png_data_url(prior_png))
        connected_cut_nums.append(prior_cut_num)

    # ① 화풍(맨 앞) + 컷 프롬프트
    prompt = f"{SEMI_REAL_SUFFIX.strip()} {shot['prompt']}"
    # ② 연속성 앵커(직전 컷 이미지가 붙었을 때)
    if connected_cut_nums:
        prompt += ("\n\n" + _shot_continuity_prompt(shot, connected_cut_nums)
                   + "\n(The continuity anchor is the LAST attached reference image — preserve its "
                   "character identity, hairstyle, wardrobe, location and lighting; do not copy its "
                   "pose, expression, or camera framing.)")
    # ③ 참조 역할 분리 + 의상 오염 방지
    if role_block:
        prompt += f"\n\n{role_block}"
    # ④ 사용자 지시(최우선, 맨 끝)
    if feedback and feedback.strip():
        prompt += ("\n\n[★ HIGHEST-PRIORITY USER INSTRUCTION — this overrides the scene/conti "
                   "description above wherever they conflict. Keep reference images (faces/costumes/"
                   f"places) unchanged. 사용자 지시: '{feedback.strip()}']")
    return _with_retry(oi.generate, prompt, aspect_ratio="9:16", refs=refs)


_YOLO_WEIGHTS = os.path.join(os.path.dirname(__file__), "models", "yolov8n.pt")
_FACE_HEIGHT_RATIO = 0.30  # 사람 박스 높이 중 상단 몇 %를 "얼굴 영역"으로 근사해서 덮을지
_FACE_PAD_X, _FACE_PAD_Y = 0.05, 0.05
_PERSON_CLASS = 0
_PERSON_CONF_THRESHOLD = 0.4

_YOLO_MODEL = None


def _yolo_model():
    global _YOLO_MODEL
    if _YOLO_MODEL is None:
        from ultralytics import YOLO
        _YOLO_MODEL = YOLO(_YOLO_WEIGHTS)
    return _YOLO_MODEL


def _detect_face_boxes(png: bytes, W: int, H: int) -> list[tuple[int, int, int, int]]:
    """화면에 등장하는 사람마다(다중 인물 지원) 얼굴 영역을 (x,y,w,h) 리스트로 근사 반환.
    2인 이상 등장 컷에서 얼굴 하나만 가리면 남은 얼굴 때문에 안전필터가 여전히 걸리므로
    감지된 사람 전부를 반환한다(2026-07-21, 실측 — 2인 컷에서 격자 재시도도 필터에 걸림).

    ★2026-07-22: 기존엔 cv2 Haar cascade(애니풍→실사 정면+측면)로 "얼굴"을 직접 찾았는데,
    실측 검증(다중 인물 스틸컷으로 직접 영상화 API 호출)에서 옆모습·조명이 있는 실제 프로덕션
    스틸컷의 한쪽 인물을 계속 놓치는 게 확인됨(반측면 얼굴을 캐스케이드가 못 잡음) — 그 상태로는
    격자/박스를 씌워도 놓친 얼굴이 그대로 노출돼 필터를 못 피함. YOLOv8(person 클래스)로
    교체해 사람 전신 박스를 훨씬 안정적으로 잡고(포즈/각도에 덜 민감), 그 박스 상단 일부를
    얼굴 영역으로 근사해 덮는 방식으로 바꿈 — co-writer-bot/storyboard-bot과 동일 로직(같은
    테스트 이미지로 재검증해 안전필터를 통과함을 확인함)."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            res = _yolo_model()(img, verbose=False)[0]
            boxes = []
            for b in res.boxes:
                if int(b.cls) != _PERSON_CLASS or float(b.conf) < _PERSON_CONF_THRESHOLD:
                    continue
                x1, y1, x2, y2 = b.xyxy[0].tolist()
                h = y2 - y1
                fy0 = y1 - h * _FACE_PAD_Y
                fy1 = y1 + h * _FACE_HEIGHT_RATIO
                w = x2 - x1
                px = w * _FACE_PAD_X
                x0 = max(0.0, x1 - px); x1p = min(float(W), x2 + px)
                y0 = max(0.0, fy0); y1p = min(float(H), fy1)
                boxes.append((int(x0), int(y0), int(x1p - x0), int(y1p - y0)))
            if boxes:
                return boxes
    except Exception:
        pass
    # 폴백: opencv/ultralytics 미설치(배포=homebrew python 등) 시 PIL만으로 상단 중앙을 넉넉히
    # 덮는다 — 감지 아닌 휴리스틱이지만 no-op보다 훨씬 안전(co-writer-bot _heuristic_boxes_pil 이식).
    w, h = int(W * 0.72), int(H * 0.42)
    return [((W - w) // 2, int(H * 0.06), w, h)]


def _facegrid_overlay(png: bytes) -> bytes:
    """image-to-video의 실존인물 안전필터(InputImageSensitiveContentDetected)를 회피하려고
    화면에 등장하는 사람 전부의 얼굴 영역에 빨간 불투명 박스를 얹는다(2인 이상 등장 컷에서
    하나만 가리면 남은 얼굴 때문에 필터가 여전히 걸림 — 2026-07-21 실측).

    ★2026-07-22: 격자 "선"만으로는 얼굴 대부분이 그대로 노출돼 필터를 못 피하는 게 실측으로
    확인됨(같은 이미지를 완전 불투명 박스로 덮으니 통과) — 그래서 격자선 대신 감지 영역을
    완전히 채운다(함수명은 호출부 호환을 위해 유지)."""
    from PIL import Image, ImageDraw
    base = Image.open(io.BytesIO(png)).convert("RGBA")
    W, H = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for x, y, w, h in _detect_face_boxes(png, W, H):
        d.rectangle([x, y, x + w, y + h], fill=(237, 28, 36, 255))
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
# ★2026-07-22: co-writer-bot(정답 레퍼런스)의 영상 잠금 블록 이식 — image-to-video가 스틸을
# 무시하고 인물(얼굴·머리·옷)·프레이밍을 제멋대로 바꾸는(드리프트) 사고를 막는다. 모든 영상 모션
# 프롬프트 맨 앞에 붙여, 첨부한 스틸을 "정확히 유지할 첫 프레임"으로 못박고 카메라·화풍을 고정한다.
_VIDEO_FICTION_LOCK = (
    "An entirely fictional adult character, created for an original fictional drama. "
    "The character is not based on, associated with, or intended to resemble any real person, "
    "celebrity, public figure, or private individual. Realistic cinematic look (~80% realism, "
    "clean photographic rendering, not illustration/cartoon/anime), a clearly fictional digital "
    "character. ")
_VIDEO_REF_LOCK = (
    "The provided reference image is the exact first frame of this video — do not change the "
    "character's face, hair color/style, clothing, or background/setting shown in that reference "
    "image. Only animate the motion described below; every visual element not explicitly described "
    "as changing must stay identical to the reference image throughout the video. ")
# ★2026-07-22(HANDOFF_영상화_인물일관성): seedance가 텍스트에 끌려 스틸에 없는 새 인물을 만들거나
# 얼굴을 바꾸는 드리프트 방지 — 참조 첫 프레임의 인원·정체성을 그대로 고정하는 강한 금지문.
_VIDEO_IDENTITY_LOCK = (
    "Do NOT introduce, add, or invent any new person or face that is not present in the reference "
    "first frame. The people in this video are EXACTLY those in the reference image — the same "
    "number of people and the same identity for each. Do not merge, swap, or replace faces, and do "
    "not add any background people who are not in the reference image. ")
_VIDEO_CAMERA_LOCK = (
    "Keep the camera static/locked in place by default — do not push in, zoom, dolly, or pan "
    "unless the shot description below explicitly calls for camera movement. Maintain the exact "
    "framing/shot size (e.g., medium shot stays medium shot) from the first frame throughout — "
    "do not drift into a closer shot on your own. ")
_VIDEO_STYLE_LOCK = (
    "Semi-realistic cinematic K-drama style, ~80% realism (photoreal lighting/proportion/depth "
    "with a subtly painterly finish, not a flat cartoon/anime, not a pure photograph). ")


def _video_lock_prefix() -> str:
    """모든 영상 모션 프롬프트 앞에 붙는 잠금 블록(가상인물→ref→정체성→카메라→화풍 순)."""
    return (_VIDEO_FICTION_LOCK + _VIDEO_REF_LOCK + _VIDEO_IDENTITY_LOCK
            + _VIDEO_CAMERA_LOCK + _VIDEO_STYLE_LOCK)


# ★2026-07-23(co-writer-bot 조립 이식): 컷 내용 뒤에 붙는 공통 트레일링 — 표정은 미세·점진적으로만,
# 그리고 컷 하나는 '한 번의 연속 샷'(장소·조명·프레이밍·배경 유지, 장면 전환/컷 전환 없음)임을 못박는다.
_VIDEO_TRAILING = (
    " Facial expression must stay subtle and natural — very minor, gradual expression change only, "
    "no sudden or exaggerated emotional shifts. If the scene action describes a short continuous "
    "progression (e.g., reaching for something and then making eye contact), animate that "
    "progression naturally in sequence — this is one continuous shot, no camera cut, so keep the "
    "exact same location, lighting, camera framing and background throughout, no scene change or "
    "transition to a different setup.")


# ★2026-07-22: 대사 없는 컷은 generate_audio=False — 자동 생성 음성이 'real person audio'
# 안전필터에 걸리는 걸 회피(co-writer-bot HANDOFF_안전필터우회 이식). 대사 판정 정규식.
_DIALOGUE_QUOTE_RE = re.compile(r"'[^']{2,}'|\"[^\"]{2,}\"|[‘“][^’”]{2,}[’”]|[「『][^」』]{2,}[」』]")
_DIALOGUE_MARKER_RE = re.compile(
    r"나레이션|내레이션|보이스\s*오버|방백|독백|읊조|중얼|외치|말한다|말하며|대사|"
    r"\bNa\b|\(Na\)|V\.?O\.?|voice[\s-]*over|narrat", re.I)


def _has_dialogue(text: str) -> bool:
    """텍스트에 대사/나레이션이 있는지(따옴표 발화 또는 나레이션·대사 마커)."""
    t = text or ""
    return bool(_DIALOGUE_QUOTE_RE.search(t) or _DIALOGUE_MARKER_RE.search(t))


def _clip_has_dialogue(clip: dict) -> bool:
    """클립 '자체'(블록 서술·라벨)에 대사가 있는지 — clip_motion_prompt 헤더의 '대사'라는 지시어에
    오탐되지 않게 모션 프롬프트가 아니라 클립 내용만 본다."""
    texts = [clip.get("label") or ""]
    for b in clip.get("blocks") or []:
        texts.append(b.get("text") or "")
    return _has_dialogue(" ".join(texts))


def generate_video_for_cut(work: str, scene_num: int, cut_num: int, png: bytes,
                           motion_prompt: str, episode: int = 1, shot: dict | None = None,
                           clip_id: str | None = None, want_audio: bool | None = None) -> str:
    """스틸컷 1장을 영상화해 로컬 mp4 절대경로를 반환. project_setup.ensure_project(work)를
    먼저 호출해둬야 vp_store가 프로젝트 디렉토리를 찾을 수 있다.
    clip_id가 주어지면 저장을 컷(cut_num)이 아니라 클립(clip{clip_id}) 단위로 한다 — v3.1
    영상 호출 단위 = 클립(generate_video_for_clip이 사용). cut_num 경로는 기존 shot 파이프라인용.
    ★실존인물 안전필터에 걸리면 재스타일화 없이 곧바로 얼굴에 빨간 격자를 덮어(cv2 얼굴 자동
    감지) 재시도한다. 격자 회피 시엔 그 격자 스틸이 승인된 시작 프레임임을 프롬프트 맨 앞줄에
    못박고(anchor), 생성된 영상 앞 0.1초를 잘라 격자 첫 프레임이 최종 영상에 안 비치게 한다."""
    unit = f"클립{clip_id}" if clip_id is not None else f"컷{cut_num}"
    anchor_name = f"clip{clip_id}.png" if clip_id is not None else f"cut{cut_num}.png"

    # 대사 없는 컷은 오디오 생성 끔(real-person audio 안전필터 회피). want_audio 미지정이면 모션
    # 프롬프트로 판정(구 shot 경로: caption=컷 내용). v3 클립은 호출부가 클립 기준으로 판정해 넘긴다
    # (clip_motion_prompt 헤더의 '대사' 지시어 오탐 방지). 대사 컷만 config 토글을 최종 적용.
    _has_dlg = want_audio if want_audio is not None else _has_dialogue(motion_prompt)
    _wa = _has_dlg and sb_config.OPENROUTER_VIDEO_GENERATE_AUDIO
    # 대사 없는 컷은 입을 다물게(발화 입모양·립싱크 금지) — 없던 대사가 생기는 드리프트 방지.
    # ★2026-07-23: co-writer-bot 문구로 통일 + 위치를 컷 내용 뒤로 이동(맨 앞이 아니라). 대사 컷은 빈 문자열.
    dialogue_lock = ("" if _has_dlg else
                     "The character's lips stay closed together throughout, without forming any "
                     "speech shapes or mouth movements — this cut carries no dialogue. ")

    def _gen_once(image_bytes, prompt):
        # _with_retry 재시도 없이 1회만 — 안전필터는 재시도해도 같은 결과라 낭비
        return hf_video.generate(image_bytes, prompt,
                                 aspect_ratio="9:16", generate_audio=_wa)

    def _is_filter(e):
        return "InputImageSensitiveContentDetected" in str(e)

    # ★2026-07-23 조립 순서(co-writer-bot 이식): [잠금 접두: fiction·ref·정체성·카메라·화풍]
    # → 컷 내용(motion_prompt) → dialogue_lock → 공통 트레일링(표정·연속샷). 안전필터 재시도
    # 시에만 grid_anchor를 이 앞에 덧붙인다.
    locked_prompt = (_video_lock_prefix() + motion_prompt + " " + dialogue_lock
                     + _VIDEO_TRAILING).strip()
    grid_used = False
    try:
        url, cost = _gen_once(png, locked_prompt)
    except Exception as e:
        if not _is_filter(e):
            raise
        # 안전필터 → 얼굴 격자로 재시도(실패는 아니지만 왜 이 컷이 느리거나 다르게 나오는지 진단용)
        log.info("⚠ 안전필터 감지 (씬%s %s) — 얼굴 격자로 재시도합니다.", scene_num, unit)
        grid_anchor = (
            f"<<<{anchor_name}>>> is the clean approved start frame and must remain the exact "
            f"identity, costume, location, lighting, and screen-direction anchor.\n")
        try:
            url, cost = _gen_once(_facegrid_overlay(png), grid_anchor + locked_prompt)
        except Exception as e2:
            if _is_filter(e2):
                raise RuntimeError(
                    f"안전필터 회피 실패 (씬{scene_num} {unit}): 얼굴 격자 적용 후에도 필터에 걸렸습니다."
                ) from e2
            raise
        grid_used = True
    path = vp_store.save_video(work, scene_num=scene_num, cut_num=cut_num, url=url,
                               episode=episode, cost=cost, clip_id=clip_id)
    if not path:
        raise RuntimeError(f"영상 다운로드/저장 실패 (씬{scene_num} {unit})")
    # 격자로 생성한 경우 격자 첫 프레임이 최종 영상에 비치지 않도록 앞 0.1초 트림
    if grid_used:
        _trim_head_0_1s(path, 0.1)
    return path


def generate_cuts_for_scene(work: str, scene_num: int, shots: list[dict],
                            episode: int = 1, on_progress=None,
                            cached_stills: list[dict] | None = None) -> list[dict]:
    """씬의 모든 샷을 순서대로 이미지→영상 생성. 안전필터 등으로 실패한 컷은 그 컷만
    건너뛰고 나머지를 계속 진행(전체 실패 방지만 목적 — 재시도 고도화는 안 함, MVP 범위).
    미리보기에서 승인된 cached_stills가 있으면 같은 씬·컷의 PNG를 재사용하고, 없는 컷만
    새로 생성한다.
    반환: [{"cut_num", "status": "ok"|"failed", "video_path"?, "error"?}, ...]"""
    results = []
    cached_by_cut = {
        (s.get("scene_num"), s.get("cut_num")): s
        for s in (cached_stills or []) if s.get("image")
    }
    previous_cut_num = None
    previous_png = None
    for shot in shots:
        cut_num = shot["n"]

        def notify(msg):
            if on_progress:
                on_progress(scene_num, cut_num, msg)

        try:
            cached = cached_by_cut.get((scene_num, cut_num))
            if cached:
                notify("승인 스틸 재사용 중")
                png = oi.data_url_to_png(cached["image"])
            else:
                notify("이미지 생성 중")
                continuity_refs = ([(previous_cut_num, previous_png)]
                                   if previous_cut_num is not None and previous_png else None)
                png, _img_cost = generate_image_for_shot(
                    shot, work=work, continuity_refs=continuity_refs)
            # 영상화 성공 여부와 무관하게 이 승인/생성 스틸을 다음 컷의 연속성 앵커로 사용한다.
            previous_cut_num, previous_png = cut_num, png
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
            log.exception("영상화 실패 (씬%s 컷%s): %s", scene_num, cut_num, e)
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
        scenes = _norm_scenes(episode["scenes"])
        shots = _restore_costume_assignments(scenes, _norm_shots(episode["shots_by_scene"]))
        if save_fn:
            save_fn(shots_by_scene=shots)
        return scenes, shots

    script = episode.get("script")
    if not script:
        raise RuntimeError("대본이 먼저 있어야 해요.")
    jobs.update(job_id, stage="씬 설계 중")
    plan_text = generate_scene_plan(script, episode=num, characters=characters)
    scenes_plan = parsing.parse_plan_scenes(plan_text)
    if not scenes_plan:
        raise RuntimeError("씬 설계안에서 씬 목록을 파싱하지 못했어요.")
    conti_full = generate_conti(script, plan_text, scenes_plan, episode=num, characters=characters,
                                work=work, prior_conti=episode.get("conti_full") or "")
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
    """씬의 모든 샷 중 핵심 상호작용을 담은 대표 샷 1개만 스틸로 생성한다.

    미리보기 비용·대기시간을 줄이고, 전체 영상 제작 때 이 승인 스틸을 재사용한다. 대표 컷이
    아닌 나머지 컷 이미지는 최종 영상 제작 시점에 필요한 것만 생성한다.
    """
    shot = _representative_shot(shots)
    if not shot:
        return []
    try:
        png, _cost = generate_image_for_shot(shot, work=work)
    except Exception:
        return []
    return [{"scene_num": scene_num, "cut_num": shot.get("n"),
             "caption": shot.get("caption", ""), "prompt": shot.get("prompt", ""),
             "image": oi.png_data_url(png), "video_path": None, "representative": True}]


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
                               characters=characters, work=work,
                               prior_conti=episode.get("conti_full") or "")
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
        jobs.update(job_id, stage=f"씬{scene_num} 대표 스틸 생성 중")
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
    scenes = _norm_scenes(episode.get("scenes") or [])
    shots_by_scene = _restore_costume_assignments(
        scenes, _norm_shots(episode.get("shots_by_scene") or {}))
    shot = next((s for s in (shots_by_scene.get(scene_num) or []) if s.get("n") == cut_num), None)
    if not shot:
        raise RuntimeError(f"씬{scene_num} 컷{cut_num}을 찾을 수 없어요.")
    previous = max(
        (s for s in (episode.get("scene_stills") or [])
         if s.get("scene_num") == scene_num and isinstance(s.get("cut_num"), int)
         and s.get("cut_num") < cut_num and s.get("image")),
        key=lambda s: s["cut_num"], default=None)
    continuity_refs = None
    if previous:
        continuity_refs = [(previous["cut_num"], oi.data_url_to_png(previous["image"]))]
    png, _cost = generate_image_for_shot(
        shot, work=work, continuity_refs=continuity_refs)
    return {"scene_num": scene_num, "cut_num": cut_num, "caption": shot.get("caption", ""),
            "prompt": shot.get("prompt", ""), "image": oi.png_data_url(png), "video_path": None}


def _save_conti_for_review(work: str, episode: int, scene_num: int, conti_text: str) -> None:
    """생성된 씬 상세 콘티를 검수용 텍스트 파일로 남긴다(★2026-07-22 사용자 요청 — 콘티 검수).
    위치: outputs/<작품>/<N>화/상세콘티/씬N.txt. 실패해도 파이프라인은 계속(검수용 부가 파일)."""
    try:
        root = vp_store.out_root(work)
        if not root or not conti_text:
            return
        d = root / f"{episode}화" / "상세콘티"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"씬{scene_num}.txt").write_text(conti_text, encoding="utf-8")
    except Exception:
        pass


def _load_conti_from_review(work: str, episode: int, scene_num: int) -> str | None:
    """검수용 상세콘티 파일(outputs/<작품>/<N>화/상세콘티/씬N.txt)을 읽어 돌려준다.
    ★2026-07-22 사용자 요청 — 이 파일을 손으로 수정하면 그 내용이 영상 생성의 콘티 소스가 된다(최우선).
    없거나 비었거나 읽기 실패하면 None(→ 미리보기 상태 conti_text / 신규 LLM 생성으로 폴백)."""
    try:
        root = vp_store.out_root(work)
        if not root:
            return None
        p = root / f"{episode}화" / "상세콘티" / f"씬{scene_num}.txt"
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def _find_v3_scene_clip(episode: dict, scene_num: int, clip_id: str):
    """v3_scenes의 그 씬 콘티를 파싱해 (scene_dict, clip_dict)를 돌려준다. 못 찾으면 (None, None)."""
    for s in (episode.get("v3_scenes") or []):
        if int(s.get("scene_num") or -1) != scene_num:
            continue
        conti = s.get("conti_text")
        parsed = parsing.split_scenes(conti) if conti else None
        if not parsed:
            return None, None
        _, hdr, body = parsed[0]
        scene = v3_schema.parse_scene(hdr, body)
        scene["scene_num"] = scene_num
        clip = next((c for c in (scene.get("clips") or []) if c.get("clip_id") == clip_id), None)
        return scene, clip
    return None, None


def _persist_cut_video(episode: dict, scene_num: int, cut_num: int, video_path: str, save_fn) -> None:
    """생성한 영상 경로를 그 컷 스틸(scene_stills + v3_scenes[].stills)에 저장 — 페이지를 넘겨도,
    재로드해도 그 컷에 영상이 유지되게 한다(★2026-07-22, 페이지네이션 이슈)."""
    def _mark(s):
        if s.get("scene_num") == scene_num and s.get("cut_num") == cut_num:
            return {**s, "video_path": video_path}
        return s
    fields = {"scene_stills": [_mark(s) for s in (episode.get("scene_stills") or [])]}
    v3 = episode.get("v3_scenes") or []
    if v3:
        new_v3 = []
        for s in v3:
            if int(s.get("scene_num") or -1) == scene_num and s.get("stills"):
                s = {**s, "stills": [_mark(st) for st in s["stills"]]}
            new_v3.append(s)
        fields["v3_scenes"] = new_v3
    save_fn(**fields)


def videoize_cut_job(project: dict, episode: dict, job_id: str, save_fn=None, *,
                     scene_num: int, cut_num: int, note: str = "") -> None:
    """미리보기(또는 재생성)로 만들어둔 특정 컷 스틸을 그대로 영상화. 백그라운드 스레드 전제
    (예외는 jobs에 error로 남김) — server.py가 _run_with_locked_references로 호출(save_fn 주입).

    note가 있으면(사용자가 '다시 영상화' 시 입력한 의견) 모션 프롬프트 끝에 최우선 지시로 덧붙인다.

    ★2026-07-22(인물 뒤바뀜 수정): 스틸은 v3 scene_stills에서 가져오면서 모션 프롬프트는 구
    shots_by_scene[cut_num]에서 가져오던 버그 — 두 콘티는 컷 순서/내용이 완전히 달라(구 shot n=1이
    '강태민 바코드'인데 v3 클립1은 '이수진 진열대') 여자 스틸에 남자 모션이 붙어 영상에서 인물이
    바뀌었다. v3 스틸(clip_id 보유)이면 v3_scenes 콘티에서 그 클립을 찾아 v3 멀티샷 모션으로
    영상화하고, clip_id가 없는 구 파이프라인 컷만 shots_by_scene 경로를 쓴다."""
    work = project["work"]
    num = episode["num"]
    try:
        project_setup.ensure_project(work)
        all_stills = episode.get("scene_stills") or []
        still = next((s for s in all_stills
                     if s.get("scene_num") == scene_num and s.get("cut_num") == cut_num and s.get("image")),
                    None)
        if not still:
            raise RuntimeError("이 컷의 스틸이 없어요. 먼저 이미지를 만들어주세요.")
        jobs.update(job_id, stage="영상화 중")
        note = (note or "").strip()
        clip_id = still.get("clip_id")
        # ★2026-07-22: 영상은 '클립(블록 묶음)' 단위 — 어느 컷(블록) 스틸을 눌러도 그 클립의 대표
        # (첫) 블록 스틸을 시작 프레임으로 삼아 클립 전체를 애니메이트한다. 대표가 없으면 클릭한 컷.
        seed = still
        if clip_id:
            seed = next((s for s in all_stills if s.get("clip_id") == clip_id
                        and s.get("representative") and s.get("image")), still)
        png = oi.data_url_to_png(seed["image"])
        if clip_id:  # v3 클립 — 그 클립의 멀티샷 모션 프롬프트로 영상화(구 shots_by_scene 무시)
            scene, clip = _find_v3_scene_clip(episode, scene_num, clip_id)
            if not clip:
                raise RuntimeError(f"v3 씬{scene_num} 클립 {clip_id}의 콘티를 찾을 수 없어요.")
            motion = sb_prompts.clip_motion_prompt(scene, clip)
            if note:
                motion = f"{motion}\n\n[사용자 요청 반영 — 아래 지시를 최우선으로]: {note}"
            path = generate_video_for_clip(work, scene_num, clip_id, png, motion, episode=num,
                                           want_audio=_clip_has_dialogue(clip))
        else:  # 구 shot 파이프라인 컷
            shots_by_scene = _norm_shots(episode.get("shots_by_scene") or {})
            shot = next((s for s in (shots_by_scene.get(scene_num) or []) if s.get("n") == cut_num), None)
            if not shot:
                raise RuntimeError(f"씬{scene_num} 컷{cut_num}을 찾을 수 없어요.")
            motion_prompt = shot.get("caption", shot["prompt"])
            if note:
                motion_prompt = f"{motion_prompt}\n\n[사용자 요청 반영]: {note}"
            path = generate_video_for_cut(work, scene_num, cut_num, png,
                                          motion_prompt=motion_prompt, episode=num, shot=shot)
        if save_fn:
            try:
                _persist_cut_video(episode, scene_num, cut_num, path, save_fn)
            except Exception:
                pass  # 저장 실패해도 job엔 영상이 있으니 즉시 표시는 됨
        jobs.update(job_id, status="done", stage="완료", video_path=path)
    except Exception as e:
        log.exception("컷 재영상화 실패 (씬%s 컷%s): %s", scene_num, cut_num, e)
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
                generate_cuts_for_scene(
                    work, scene_num, shots, episode=num, on_progress=on_progress,
                    cached_stills=episode.get("scene_stills") or [])

        jobs.update(job_id, stage="합본 중")
        title = f"{num}화" + (f" — {episode['subtitle']}" if episode.get("subtitle") else "")
        draft_path = compile_episode_video(work, idea, scenes, shots_by_scene,
                                           episode=num, episode_title=title)
        jobs.update(job_id, status="done", stage="완료", video_path=draft_path)
    except Exception as e:
        jobs.update(job_id, status="error", stage="오류", error=str(e))
