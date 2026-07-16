# drama-mvp

한 줄 스토리 아이디어 → 완전 자동으로 기획안·대본·씬설계·콘티·샷분해·이미지·영상·합본까지 만들어주는 모바일 웹 데모.

내부/투자자 시연용 MVP. `co-writer-bot`, `storyboard-bot`의 생성 로직을 `vendor/`에 복사해 재사용한다 (원본과 별도 관리, 자동 동기화 없음).

## 구조

```
vendor/cowriter/     # co-writer-bot에서 복사한 기획안·대본 생성 모듈 + 레퍼런스 데이터
vendor/storyboard/   # storyboard-bot에서 복사한 씬설계~합본 생성 모듈
pipeline/            # 신규: 파싱 유틸 + 오케스트레이터 (Slack 없이 전체 파이프라인 실행)
server.py            # FastAPI (예정)
static/              # 모바일 웹 프론트 (예정)
cli_test.py           # 터미널에서 파이프라인 단독 실행/검증
```

## 실행

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # OPENROUTER_API_KEY 등 입력
python3 cli_test.py "한 줄 아이디어"
```
