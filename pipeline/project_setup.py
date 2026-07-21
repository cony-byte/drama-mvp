# -*- coding: utf-8 -*-
"""storyboard-bot의 openrouter_image._vp_project_dir()가 작품을 찾으려면
FIXED_IMAGES_ROOT/<work>/project.json이 있어야 하는데, app.py는 이 파일이 이미 수동으로
온보딩돼 있다고 가정해서 만드는 코드가 없다. 데모용 작품마다 이 스켈레톤을 새로 만든다."""
import json
from pathlib import Path

import vendor.storyboard.bot.config as sb_config


def ensure_project(work: str) -> Path:
    proj_dir = sb_config.FIXED_IMAGES_ROOT / work
    proj_dir.mkdir(parents=True, exist_ok=True)
    project_json = proj_dir / "project.json"
    if not project_json.exists():
        project_json.write_text(json.dumps({
            "project": {
                "slug": work,
                "work_name": work,
                "project_root": str(proj_dir),
                "status": "draft",
            },
            "shared_paths": {
                "fixed_images_root": "fixed-images",
                "generated_images_root": "generated",
                "outputs_root": "outputs",
                "logs_root": "logs",
                "database": "visual.db",
            },
            "characters": [],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return proj_dir
