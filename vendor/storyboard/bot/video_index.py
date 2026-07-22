# -*- coding: utf-8 -*-
"""화(에피소드) 단위 영상 인덱싱 — <프로젝트>/outputs/videos/<화>/에 이미 생성된 컷별 영상을
씬 번호로 그룹핑해 합본(episode_compile)이 쓸 수 있게 해준다.

visual.db의 generations.scene은 "씬N" 문자열만 있고 cut 번호가 없어서(vp_store.py 참고),
DB 조회보다 vp_store.save_video가 쓰는 파일명 패턴(video_s{scene}_cut{cut}_{uuid8}.mp4)을
직접 스캔하는 게 더 간단하고 정확하다.

★2026-07-14: 화 구분 없이 outputs/videos/ 밑에 평평하게 저장하면 서로 다른 화가 같은 씬
번호를 쓸 때 컷이 섞인다 — vp_store.save_video가 이제 outputs/videos/<화>/ 하위 폴더에
저장하므로, 여기도 화를 지정하면 그 폴더만 스캔한다. episode를 안 주면(구버전 폴더 구조
호환 목적) 하위 폴더 전체 + 레거시 평평한 파일까지 다 스캔한다."""
from __future__ import annotations

import re
from pathlib import Path

from . import openrouter_image as oi
from . import vp_store

_VIDEO_FNAME_RE = re.compile(r"^video_s(\d+)_cut(\d+)_")


def _episode_video_dirs(work: str, episode: int | str | None):
    """스캔할 영상 폴더들. episode 지정 시 그 화의 영상 폴더 하나, 아니면 모든 화의 영상 폴더.
    (★2026-07-22 outputs 재설계: outputs/<작품>/<N>화/영상/<씬>/ — 씬 하위폴더는 rglob으로 훑음.)"""
    if episode:
        d = vp_store.episode_kind_dir(work, episode, "video")
        return [d] if d and d.is_dir() else []
    root = vp_store.out_root(work)
    if not root or not root.exists():
        return []
    dirs = []
    for ep_dir in root.iterdir():
        vd = ep_dir / vp_store._KIND_FOLDER["video"]
        if vd.is_dir():
            dirs.append(vd)
    return dirs


def list_episode_videos(work: str, scene_nums: list[int] | None = None,
                        episode: int | str | None = None) -> dict[int, list[dict]]:
    """outputs/<작품>/<N>화/영상/<씬>/를 스캔해 {scene_num: [{"cut_num", "path", "mtime"}, ...]}로
    반환. 같은 (scene, cut)이 여러 번 재생성됐으면 가장 최근(mtime 최대) 것만 남긴다.
    scene_nums를 주면 그 씬들만 필터링(없으면 전체). episode를 주면 그 화만, 안 주면 전체."""
    latest: dict[tuple[int, int], dict] = {}
    for d in _episode_video_dirs(work, episode):
        for p in d.rglob("*.mp4"):   # 씬 하위폴더까지 훑음
            if not p.is_file():
                continue
            m = _VIDEO_FNAME_RE.match(p.name)
            if not m:
                continue
            scene_num, cut_num = int(m.group(1)), int(m.group(2))
            if scene_nums is not None and scene_num not in scene_nums:
                continue
            mtime = p.stat().st_mtime
            key = (scene_num, cut_num)
            if key not in latest or mtime > latest[key]["mtime"]:
                latest[key] = {"cut_num": cut_num, "path": str(p), "mtime": mtime}
    out: dict[int, list[dict]] = {}
    for (scene_num, _cut_num), v in latest.items():
        out.setdefault(scene_num, []).append(v)
    for scene_num in out:
        out[scene_num].sort(key=lambda v: v["cut_num"])
    return out
