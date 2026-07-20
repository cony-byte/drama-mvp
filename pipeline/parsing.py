# -*- coding: utf-8 -*-
"""storyboard-bot/app.py의 순수 파싱 함수 복사본(Slack 비의존) + drama-mvp 자체 파싱 유틸.
원본: _repair_json_quotes(694 근처), _parse_json_array(694), _SCENE_HDR_RE(1612),
_PLAN_SCENE_LINE_RE(2221), _parse_plan_scenes(2224), _split_scenes(2257).
parse_json_object는 신규(로그라인+인물 카드처럼 배열이 아닌 객체 응답 파싱용)."""
import json
import re

SCENE_HDR_RE = re.compile(r"(?m)^[ \t]*(?:[■*#\-]+[ \t]*)?씬\s*(\d+)\b[^\n]*$")
PLAN_SCENE_LINE_RE = re.compile(r"(?m)^(\d+)\.\s*.+$")


def repair_json_quotes(s: str) -> str:
    """문자열 값 안의 이스케이프 안 된 큰따옴표를 복구."""
    out, in_str, i, n = [], False, 0, len(s)
    while i < n:
        c = s[i]
        if not in_str:
            out.append(c)
            if c == '"':
                in_str = True
        else:
            if c == '\\' and i + 1 < n:
                out.append(c); out.append(s[i + 1]); i += 2; continue
            if c == '"':
                j = i + 1
                while j < n and s[j] in ' \t\r\n':
                    j += 1
                if j >= n or s[j] in ',:]}':
                    out.append(c); in_str = False
                else:
                    out.append('\\"')
            else:
                out.append(c)
        i += 1
    return ''.join(out)


def parse_json_array(text: str) -> list:
    t = str(text).strip()
    s, e = t.find("["), t.rfind("]")
    if s == -1 or e == -1 or e < s:
        raise ValueError("응답에서 JSON 배열([...])을 못 찾았어요.")
    body = t[s:e + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads(repair_json_quotes(body))


def parse_json_object(text: str) -> dict:
    """parse_json_array와 같은 패턴이지만 JSON 배열이 아니라 객체({...}) 하나를 뽑는다."""
    t = str(text).strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1 or e < s:
        raise ValueError("응답에서 JSON 객체({...})를 못 찾았어요.")
    body = t[s:e + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads(repair_json_quotes(body))


def parse_plan_scenes(plan_text: str) -> list[tuple[int, str]]:
    """씬 설계안 텍스트("N. ..." 한 줄씩)에서 (씬번호, 그 줄 전문) 목록을 뽑는다."""
    out = []
    for m in PLAN_SCENE_LINE_RE.finditer(plan_text or ""):
        out.append((int(m.group(1)), m.group(0).strip()))
    return out


def split_scenes(conti: str) -> list[tuple[int, str, str]]:
    """콘티를 '■ 씬N ...' 헤더 기준으로 분할 → [(num, header, body), ...]."""
    if not conti:
        return []
    ms = list(SCENE_HDR_RE.finditer(conti))
    out = []
    for i, m in enumerate(ms):
        num = int(m.group(1))
        hdr = m.group(0).strip().strip("■*# -").strip()
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(conti)
        body = conti[start:end].strip()
        out.append((num, hdr, body))
    return out
