#!/usr/bin/env bash
# 맥미니 코드 업데이트: git pull 후 백엔드(uvicorn)만 재시작. cloudflared는 건드리지 않아
# 터널 URL이 그대로 유지된다(테스터가 서버 주소 다시 입력할 필요 없음).
# 사용: bash scripts/update_macmini.sh   (repo 루트에서)
set -euo pipefail
cd "$(dirname "$0")/.."
RUN="$(pwd)/.run"; mkdir -p "$RUN"

echo "▶ git pull…"
git pull --ff-only

echo "▶ 의존성 갱신(변경 있으면)…"
.venv/bin/pip install -q -r requirements.txt || true

echo "▶ uvicorn만 재시작(터널은 유지)…"
pkill -f "uvicorn server:app" 2>/dev/null || true
sleep 2
nohup .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 > "$RUN/uvicorn.log" 2>&1 &
sleep 5
curl -s -o /dev/null -w "  백엔드: HTTP %{http_code}\n" --max-time 10 http://127.0.0.1:8000/api/studio || true

if pgrep -f "cloudflared tunnel --url http://127.0.0.1:8000" >/dev/null; then
  echo "✅ 완료. 터널 유지됨:"
  cat "$RUN/tunnel_url.txt" 2>/dev/null || echo "  (URL 파일 없음 — cloudflared 로그 확인)"
else
  echo "⚠️ cloudflared가 안 떠 있어요. 터널을 새로 띄우려면: bash scripts/run_macmini.sh (URL 바뀜)"
fi
