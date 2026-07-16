#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""웹 없이 터미널에서 파이프라인 단독 실행/검증. 사용법: python3 cli_test.py "한 줄 아이디어"."""
import sys

from pipeline.orchestrator import run_text_stages


def main():
    if len(sys.argv) < 2:
        print('사용법: python3 cli_test.py "한 줄 아이디어"')
        sys.exit(1)
    idea = sys.argv[1]

    def on_stage(stage):
        print(f"\n=== {stage} ===", flush=True)

    result = run_text_stages(idea, on_stage=on_stage)

    print("\n\n########## 기획안 ##########")
    print(result["pitch"])
    print("\n\n########## 대본 ##########")
    print(result["script"])
    print("\n\n########## 씬 설계안 ##########")
    print(result["plan_text"])
    print("\n\n########## 상세 콘티 (씬 분할) ##########")
    for num, hdr, body in result["scenes"]:
        print(f"\n--- 씬{num}: {hdr} ---")
        print(body[:300] + ("..." if len(body) > 300 else ""))
    print("\n\n########## 샷 분해 ##########")
    for num, shots in result["shots_by_scene"].items():
        print(f"\n씬{num}: {len(shots)}개 샷")
        for s in shots:
            print(f"  #{s['n']} {s.get('caption', '')} — {s.get('prompt', '')[:80]}")


if __name__ == "__main__":
    main()
