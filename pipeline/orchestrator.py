# -*- coding: utf-8 -*-
"""한 줄 아이디어 → 기획안 → 대본 → 씬설계 → 상세콘티 → 샷분해 (1~5단계, 텍스트만).
이미지·영상·합본(6~8단계)은 나중에 이어붙인다. 모든 LLM/HTTP 호출은 vendor의 기존 함수 재사용."""
import re

import vendor.cowriter.bot.generator as cw_generator
import vendor.cowriter.bot.prompts as cw_prompts
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
    raw = _with_retry(cw_generator.complete, CHAT_SYSTEM, convo).strip()
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
    return _with_retry(cw_generator.complete, cw_prompts.plan_system(idea), user_msg).strip()


LOGLINE_SYSTEM = """너는 숏폼 로맨스 드라마 로그라인만 짧게 뽑아주는 카피라이터다.
주어진 내용을 바탕으로 후킹되는 로그라인을 **한두 문장**으로만 써라. 제목·해시태그·설명·
목록은 쓰지 말고, 로그라인 문장 그 자체만 출력해라."""


def generate_logline(idea: str) -> str:
    """전체 기획안(등장인물·줄거리·회차분배 등) 대신 로그라인 한두 문장만 먼저 보여줄 때 사용."""
    user_msg = f"다음 내용을 바탕으로 로그라인만 써줘:\n{idea}"
    return _with_retry(cw_generator.complete, LOGLINE_SYSTEM, user_msg).strip()


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
        raw = cw_generator.complete(PITCH_CARD_SYSTEM, user_msg).strip()
        return parsing.parse_json_object(raw)
    return _with_retry(_once)


SYNOPSIS_SYSTEM = """너는 숏폼 로맨스 드라마 작가다. 주어진 로그라인·등장인물을 바탕으로
전체 줄거리(시놉시스)를 3~5문단으로 써라 — 처음(발단)부터 끝(결말)까지 이야기 전체 흐름을
요약한다: 어떻게 만나는지, 중간에 어떤 갈등·반전이 있는지, 위기가 어떻게 최고조에 달하는지,
결말이 어떻게 나는지까지. 등장인물 이름은 주어진 그대로 써라. 회차 구성표나 항목 목록이
아니라 이야기를 서술하는 문단 형태로, 제목·헤더 없이 본문만 출력해라."""


def generate_synopsis(idea: str, logline: str, characters: list[dict]) -> str:
    """스튜디오의 "전체 줄거리" — 개별 화 대본과 달리 작품 전체를 관통하는 이야기 흐름.
    이후 화별 대본 생성 시 바이블처럼 참고할 수 있는 기준점이 된다."""
    names = ", ".join(f"{c.get('name')}({c.get('role')})" for c in characters)
    user_msg = f"아이디어: {idea}\n로그라인: {logline}\n등장인물: {names}\n\n전체 줄거리를 써줘."
    return _with_retry(cw_generator.complete, SYNOPSIS_SYSTEM, user_msg).strip()


EPISODE_SUMMARY_SYSTEM = """너는 숏폼 로맨스 드라마 편집자다. 주어진 한 화 분량의 대본을 읽고
그 화의 줄거리 요약을 2~3문장으로 써라 — 이 화 안에서 어떤 사건이 벌어지고 어떻게 끝나는지
(엔딩 훅 포함)만 짧게. 대본에 없는 내용을 지어내지 마라. 제목·헤더 없이 본문만 출력해라."""


def generate_episode_summary(script: str) -> str:
    """대본이 있을 때 그 대본을 2~3문장으로 요약(대본 기반)."""
    user_msg = f"다음 대본의 줄거리를 2~3문장으로 요약해줘:\n{script}"
    return _with_retry(cw_generator.complete, EPISODE_SUMMARY_SYSTEM, user_msg).strip()


EPISODE_PLAN_SUMMARY_SYSTEM = """너는 숏폼 로맨스 드라마 작가다. 작품 전체 줄거리(시놉시스)를 바탕으로
'이번 화에서 벌어질 사건'을 2~3문장으로 제안해라 — 이 화가 전체 이야기 중 어느 지점인지 고려해
(1화면 발단·첫 만남, 뒤로 갈수록 갈등·전개·위기·결말) 이 화 안에서 무슨 일이 일어나고 어떻게
끝나는지(엔딩 훅 포함) 쓴다. 이 요약이 이후 대본의 뼈대가 된다. 등장인물 이름은 주어진 그대로
써라. 제목·헤더 없이 본문만 출력해라."""


def generate_episode_plan_summary(num: int, logline: str, synopsis: str,
                                  characters: list[dict] | None = None) -> str:
    """대본이 아직 없을 때 — 전체 줄거리에서 '이번 화 사건'을 뽑아 요약을 먼저 만든다.
    이후 generate_script가 이 요약을 뼈대로 대본을 쓴다(요약 먼저, 대본 나중 흐름)."""
    names = ", ".join(f"{c.get('name')}({c.get('role', '')})"
                      for c in (characters or []) if c.get("name"))
    user_msg = (f"[작품 로그라인]\n{logline}\n\n[전체 줄거리]\n{synopsis or '(아직 없음)'}\n\n"
                f"[등장인물]\n{names or '(미정)'}\n\n위를 바탕으로 {num}화에서 벌어질 사건을 "
                f"2~3문장 요약으로 써줘.")
    return _with_retry(cw_generator.complete, EPISODE_PLAN_SUMMARY_SYSTEM, user_msg).strip()


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
        raw = cw_generator.complete(system, user)
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


def generate_script(idea: str, pitch: str, episode: int = 1,
                    characters: list[dict] | None = None, summary: str = "") -> str:
    """cw_generator.generate에 캐릭터 바이블(build_bible_block)을 그대로 넘기면 회차분배·
    줄거리 등 우리가 안 채운 필드 때문에 FAILSAFE가 "확인필요"로 멈춰버려서(실측) — 대본
    단계는 그 무거운 경로 대신 등장인물 이름만 직접 프롬프트에 못박아 이후 단계(씬설계·콘티)의
    바이블과 이름이 어긋나지 않게 한다.
    summary(이 화 요약)가 있으면 그걸 대본의 뼈대로 삼는다 — "요약 먼저, 대본은 그 요약대로"."""
    char_lines = ""
    if characters:
        names = "\n".join(f"- {c.get('name')}({c.get('role', '')})"
                          for c in characters if c.get("name"))
        if names:
            char_lines = f"\n\n[등장인물 — 반드시 이 이름 그대로 써라, 새 이름으로 바꾸지 마라]\n{names}"
    summary_lines = ""
    if summary and summary.strip():
        summary_lines = (f"\n\n[이번 화 요약 — 이 사건 흐름·엔딩을 그대로 대본으로 풀어써라, "
                         f"여기서 벗어나지 마라]\n{summary.strip()}")
    thread_messages = [{"role": "user",
                         "content": f"{pitch}{char_lines}{summary_lines}\n\n위를 바탕으로 {episode}화 대본을 써줘."}]
    return _with_retry(cw_generator.generate, thread_messages, idea, bible=None,
                       target_episode=episode, kind="대본").strip()


def generate_scene_plan(script: str, episode: int = 1,
                        characters: list[dict] | None = None) -> str:
    return _with_retry(
        sb_generator.complete,
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
        parts.append(_with_retry(sb_generator.complete, sys_prompt, user).strip())
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


def generate_image_for_shot(shot: dict, work: str | None = None) -> tuple[bytes, float]:
    """샷 하나의 스틸컷 생성 → (PNG bytes, cost$). work가 주어지면 요소 레지스트리(인물 얼굴·
    장소·소품·의상 참조 이미지)를 shot_refs()로 자동 매칭해 일관성을 유지한다."""
    refs = oi.shot_refs(work, shot) if work else []
    return _with_retry(oi.generate, shot["prompt"], aspect_ratio="9:16", refs=refs)


def generate_video_for_cut(work: str, scene_num: int, cut_num: int, png: bytes,
                           motion_prompt: str, episode: int = 1) -> str:
    """스틸컷 1장을 영상화해 로컬 mp4 절대경로를 반환. project_setup.ensure_project(work)를
    먼저 호출해둬야 vp_store가 프로젝트 디렉토리를 찾을 수 있다."""
    url, cost = _with_retry(hf_video.generate, png, motion_prompt,
                            aspect_ratio="9:16", generate_audio=False)
    path = vp_store.save_video(work, scene_num=scene_num, cut_num=cut_num, url=url,
                               episode=episode, cost=cost)
    if not path:
        raise RuntimeError(f"영상 다운로드/저장 실패 (씬{scene_num} 컷{cut_num})")
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
            notify("영상화 중")
            path = generate_video_for_cut(work, scene_num, cut_num, png,
                                          motion_prompt=shot.get("caption", shot["prompt"]),
                                          episode=episode)
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


def produce_episode_video(project: dict, episode: dict, job_id: str) -> None:
    """스튜디오 화 하나를 영상으로 제작 — 이미 만든 대본에서 씬설계→콘티→샷분해(없으면 생성)
    →이미지→영상→합본까지. 등록된 인물 얼굴 참조(work의 요소 레지스트리)를 그대로 써서 얼굴
    일관성을 유지한다. 백그라운드 스레드 전제(예외는 jobs에 error로 남김)."""
    work = project["work"]
    num = episode["num"]
    idea = project.get("idea") or project.get("logline") or ""
    characters = project.get("characters", [])
    try:
        project_setup.ensure_project(work)
        script = episode.get("script")
        if not script:
            raise RuntimeError("대본이 먼저 있어야 영상을 만들 수 있어요.")

        # 씬설계 → 콘티 → 요소 등록 → 샷분해 (화 상세에서 아직 안 만들었으면 여기서 생성).
        # 저장된 shots_by_scene은 JSON 왕복으로 키가 문자열이 될 수 있어, 항상 새로 만들어 타입을 확정한다.
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
        jobs.update(job_id, stage="샷 분해 중")
        shots_by_scene = generate_shots_by_scene(scenes, work=work, characters=characters)

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
