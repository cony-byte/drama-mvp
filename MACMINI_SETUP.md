# 맥미니 백엔드 이전 가이드 (사내 테스트 상시 서버)

프론트(gh-pages, `https://cony-byte.github.io/drama-mvp/`)는 그대로 두고 **백엔드 + 터널만** 맥미니로 옮긴다.
맥미니에서 아래를 **순서대로** 실행. (터미널 = 맥미니 것)

---

## 0. 사전 도구 (없을 때만)
```bash
# Homebrew 없으면 설치
which brew || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install git python@3 ffmpeg cloudflared
```

## 1. 코드 받기
```bash
mkdir -p ~/dev && cd ~/dev
git clone https://github.com/cony-byte/drama-mvp.git
cd drama-mvp
```

## 2. 파이썬 환경
```bash
cd ~/dev/drama-mvp
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 3. `.env` 복사 (★필수 — API 키. git에 없음)
현재 맥의 `~/dev/drama-mvp/.env` 파일을 맥미니의 같은 위치로 옮긴다.
- 방법: **AirDrop** 또는 USB로 `.env` 파일만 복사 → `~/dev/drama-mvp/.env` 에 둔다.
- (키가 안 들어가면 [확정]·이미지/영상 생성이 전부 실패한다.)

## 4. 데모 데이터 복사 (선택 — 지금 3개 작품/스틸/영상 그대로 보이게)
현재 맥에서 아래 3개를 맥미니의 같은 상대경로로 복사(AirDrop 폴더째 또는 rsync):
- `data/`                              (studio.json — 작품 목록)
- `vendor/storyboard/data/refs/`       (인물·의상·장소 참조 이미지)
- `vendor/storyboard/data/outputs/`    (스틸·영상·합본 — 용량 큼, 필요시만)

안 옮기면 맥미니는 **빈 상태**로 시작(테스터가 새로 온보딩부터).

## 5. 실행 (백엔드 + 터널)
```bash
cd ~/dev/drama-mvp
bash scripts/run_macmini.sh
```
→ 마지막에 **`https://xxxx.trycloudflare.com`** 주소가 출력된다.

## 6. 테스터 안내
- 테스터: `https://cony-byte.github.io/drama-mvp/` 접속 → **⚙️ 서버 주소 설정** 에 위 `trycloudflare.com` 주소 입력.
- 그러면 맥미니 백엔드로 붙어서 [확정]·제작 다 됨. **이제 이 맥은 꺼도 됨.**

---

## 참고
- `run_macmini.sh`는 uvicorn(127.0.0.1:8000, 상시운영이라 --reload 없음) + cloudflared를 백그라운드로 띄우고 로그를 `~/dev/drama-mvp/.run/`에 남긴다. 터널 URL도 거기 `tunnel_url.txt`에 저장.
- **주의**: cloudflared quick tunnel은 껐다 켜면 URL이 바뀐다 → 그때마다 테스터가 서버 주소를 다시 입력해야 함. 고정 URL이 필요하면 나중에 **named tunnel**(Cloudflare 계정+도메인)로 전환.
- 재부팅 후 자동 실행까지 원하면 launchd 등록이 필요(요청 시 추가).
- 코드 업데이트: `cd ~/dev/drama-mvp && git pull && .venv/bin/pip install -r requirements.txt` 후 `run_macmini.sh` 재실행.
