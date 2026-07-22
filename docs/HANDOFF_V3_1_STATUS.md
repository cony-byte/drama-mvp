# v3.1 파이프라인 이식 완료 핸드오프 (간단 버전)

`HANDOFF_V3_1_PIPELINE.md`(원래 계획서)의 12단계 권장 구현 순서가 **전부 완료**됐다.
이 문서는 그 계획서가 이제 "완료된 계획"이라는 걸 알리고, 새 세션이 헤매지 않게 현재
코드 위치만 짚어주는 짧은 갱신 노트다. 원래 계획서는 참고용으로 계속 남겨둔다.

## 완료 커밋 범위

```
3195172 v3.1 scene/clip 스키마 + 화 전체 뼈대 생성기 (2~3단계)   ← 이 세션에서 구현
70de744 v3.1 씬별 상세 블록 생성 + 검증/부분 재생성 (5단계)      ← 병렬 세션(Opus 4.8)
d66d648 v3.1 씬별 지연 레퍼런스 + 클립 단위 스틸·멀티샷 영상 (6·7·8단계)
2c25e21 v3.1 씬 순차 완주+연속성 핸드오프, 나레이션/오디오 분리 (9·10단계)
fe93aa7 v3.1 파이프라인을 앱에 연결 — 서버 잡·엔드포인트·프런트 (11·12단계)
8d4f500 perf: v3.1 produce가 미리보기 승인 스틸 재사용
e31131c fix: v3.1 뼈대 씬 헤더 파싱 견고화 (LLM 출력 변동성 대응)
0d07130 fix: 씬 총초와 클립 합 불일치 시 클립 합을 권위값으로
76ebb82 feat: face_grid 격자를 3×3 → 5×5 (안전필터 회피 강도↑, 가장 최근)
```

## 지금 코드에서 찾아야 할 것

- **스키마·검증**: `pipeline/v3_schema.py` — Scene/Clip 파서, 상태 머신, 시간 규칙 검증.
- **텍스트 생성**: `pipeline/orchestrator.py`
  - `generate_episode_skeleton` / `build_episode_skeleton` — 3단계(화 전체 뼈대).
  - `generate_scene_blocks` / `build_scene_blocks` — 5단계(씬별 상세 블록).
  - `produce_clip` / `generate_video_for_clip` / `generate_clip_still` — 클립 단위 스틸·영상.
  - `produce_episode_v3` / `produce_episode_v3_job` / `preview_scene_v3` — 씬 순차 오케스트레이션.
  - `compile_episode_v3` — 합본(나레이션/오디오 층 분리 반영).
  - `_facegrid_overlay` (`_GRID_CELLS = 5`) — 안전필터 회피 격자, 최근 5×5로 강화.
- **API**: `server.py`의 `/api/studio/{id}/episodes/{num}/v3/preview-scene`,
  `.../v3/produce` — v3.1 전용 엔드포인트(기존 shot 기반 `/preview-stills`,
  `/produce`, `/cuts/.../regenerate|videoize`는 구 파이프라인으로 그대로 공존).

## 구 파이프라인과의 관계

기존 shot 기반 파이프라인(`generate_stills_for_scene`, `generate_cuts_for_scene`,
컷별 재생성/영상화 버튼)은 **삭제되지 않고 그대로 남아있다** — v3.1은 별도 경로(`/v3/*`)로
추가됐다. 프런트에서 두 경로 중 뭘 기본으로 노출할지는 아직 정리가 필요할 수 있다.

## 남은 확인 사항

- 5×5 격자로 강화된 얼굴 그리드가 실제로 안전필터를 더 잘 통과하는지 **아직 실측 검증 전**
  (이 세션에서 실험하려다 중단됨 — 다음에 이어서 확인).
