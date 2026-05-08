#!/bin/bash
# =============================================================================
# showdon-yejja 실행 스크립트 (macOS)
# 더블클릭으로 GUI 실행. .app 번들 없이 폴더에서 직접 실행할 때 사용.
# =============================================================================

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${BLUE}[정보]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}  $1"; }
warn() { echo -e "${YELLOW}[알림]${NC} $1"; }
err()  { echo -e "${RED}[에러]${NC} $1"; }

on_exit() {
  local code=$?
  if [ $code -ne 0 ]; then
    echo ""
    echo -e "${RED}프로그램이 비정상 종료되었습니다. 메시지를 확인해주세요.${NC}"
    echo "아무 키나 누르면 창이 닫힙니다."
    read -n 1 -s
  fi
}
trap on_exit EXIT

# 스크립트 위치로 이동
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

clear
echo -e "${BOLD}================================================${NC}"
echo -e "${BOLD}      쇼돈 예짜 (예능 짜집기 자동 편집)${NC}"
echo -e "${BOLD}================================================${NC}"
echo ""

# ---- 1. venv 점검 ---------------------------------------------------------
if [ ! -d "venv" ]; then
  err "Python 가상환경(venv)이 없습니다."
  err "먼저 install.command 를 실행해 설치를 완료해주세요."
  exit 1
fi

if [ ! -f "gui.py" ]; then
  err "gui.py 를 찾을 수 없습니다."
  err "이 스크립트가 showdon-yejja 폴더 안에 있는지 확인해주세요."
  exit 1
fi

ok "환경 점검 완료"

# ---- 2. 업데이트 체크 (백그라운드, 최대 3초) ------------------------------
if command -v git &>/dev/null && [ -d ".git" ]; then
  ( git fetch origin main &>/dev/null ) &
  FETCH_PID=$!
  for _ in 1 2 3; do
    if ! kill -0 "$FETCH_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if ! kill -0 "$FETCH_PID" 2>/dev/null; then
    wait "$FETCH_PID" 2>/dev/null || true
    LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "")
    REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "")
    if [ -n "$LOCAL" ] && [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
      echo ""
      warn "🆕  새 버전이 있습니다. install.command 를 다시 실행해 업데이트하시는 걸 권장합니다."
      echo ""
    fi
  fi
fi

# ---- 3. yt-dlp 자동 업데이트 (선택) ---------------------------------------
# yt-dlp 는 플랫폼이 자주 바뀌어서 매번 최신화하면 안정성 ↑
# 단 인터넷 없을 때를 위해 실패 무시
# shellcheck disable=SC1091
source venv/bin/activate

(
  pip install --quiet --upgrade yt-dlp 2>/dev/null
) &
UPDATE_PID=$!
for _ in 1 2 3 4 5; do
  if ! kill -0 "$UPDATE_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done
# 5초 후에도 안 끝나면 그대로 진행 (백그라운드에 둠)

# ---- 4. GUI 실행 ----------------------------------------------------------
info "GUI 실행 중..."
echo ""
python gui.py
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  echo ""
  err "GUI 가 비정상 종료되었습니다 (exit code: $EXIT_CODE)."
  exit $EXIT_CODE
fi
