#!/usr/bin/env bash
# 맥미니 상시 백엔드 실행: uvicorn(127.0.0.1:8000) + cloudflared quick tunnel.
# 사용: bash scripts/run_macmini.sh   (repo 루트에서)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
RUN="$ROOT/.run"
mkdir -p "$RUN"

if [ ! -x ".venv/bin/uvicorn" ]; then
  echo "❌ .venv가 없어요. MACMINI_SETUP.md의 2단계(venv+requirements)를 먼저 하세요."; exit 1
fi
if [ ! -f ".env" ]; then
  echo "⚠️  .env가 없습니다. API 키가 없으면 확정·생성이 전부 실패해요(3단계 참고). 그래도 계속 띄웁니다."
fi
command -v cloudflared >/dev/null || { echo "❌ cloudflared 미설치: brew install cloudflared"; exit 1; }

echo "▶ 기존 프로세스 정리…"
pkill -f "uvicorn server:app" 2>/dev/null || true
pkill -f "cloudflared tunnel --url http://127.0.0.1:8000" 2>/dev/null || true
sleep 2

echo "▶ 백엔드(uvicorn) 시작 → $RUN/uvicorn.log"
nohup .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 > "$RUN/uvicorn.log" 2>&1 &
sleep 5
curl -s -o /dev/null -w "  백엔드 상태: HTTP %{http_code}\n" --max-time 10 http://127.0.0.1:8000/api/studio || true

echo "▶ 터널(cloudflared) 시작 → $RUN/cloudflared.log"
nohup cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate > "$RUN/cloudflared.log" 2>&1 &

echo "▶ 터널 URL 대기…"
URL=""
for i in $(seq 1 20); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$RUN/cloudflared.log" 2>/dev/null | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done

if [ -n "$URL" ]; then
  echo "$URL" > "$RUN/tunnel_url.txt"
  echo ""
  echo "======================================================================"
  echo "✅ 서버 주소(테스터가 '서버 주소 설정'에 입력):"
  echo "   $URL"
  echo "======================================================================"
  echo "프론트: https://cony-byte.github.io/drama-mvp/  → ⚙️ 서버 주소 설정 → 위 주소"
  echo "로그: $RUN/uvicorn.log , $RUN/cloudflared.log"
else
  echo "⚠️ 터널 URL을 못 찾았어요. $RUN/cloudflared.log 를 확인하세요."
  tail -20 "$RUN/cloudflared.log" 2>/dev/null || true
fi
