#!/bin/bash
# =============================================================================
# showdon-yejja 설치 스크립트 (macOS)
# 더블클릭으로 실행.
# =============================================================================

set -uo pipefail

REPO_URL="https://github.com/crack00000100000/showdon-yejja.git"
# 설치 경로 — ~/Documents/ 는 macOS TCC 보호 대상이라 .app 더블클릭 시 권한 문제 발생.
# 홈 아래 비보호 경로 (~/showdon/) 에 두면 .app 정상 동작.
INSTALL_DIR="$HOME/showdon/showdon-yejja"
PYTHON_BIN="python3.13"

# 스크립트가 기존 설치 폴더 안에서 실행됐을 수 있으므로 cwd 를 안전한 곳으로 이동
cd "$HOME"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

step()    { echo ""; echo -e "${BOLD}${BLUE}>>> $1${NC}"; }
info()    { echo -e "${BLUE}[정보]${NC} $1"; }
ok()      { echo -e "${GREEN}[완료]${NC} $1"; }
warn()    { echo -e "${YELLOW}[경고]${NC} $1"; }
err()     { echo -e "${RED}[에러]${NC} $1"; }

on_exit() {
  local code=$?
  echo ""
  if [ $code -eq 0 ]; then
    echo -e "${GREEN}${BOLD}설치 완료!${NC}  아무 키나 누르면 창이 닫힙니다."
  else
    echo -e "${RED}${BOLD}설치가 중단되었습니다.${NC}  메시지를 확인하시고 아무 키나 누르세요."
  fi
  read -n 1 -s
}
trap on_exit EXIT

clear
echo "================================================"
echo "    showdon-yejja (쇼돈 예짜) 설치"
echo "================================================"
echo ""
echo "이 스크립트는 자동으로 필요한 프로그램을 점검하고 설치합니다."
echo "도중에 두 번 사용자 액션이 필요할 수 있습니다:"
echo "  • Xcode 다이얼로그에서 '설치' 버튼 클릭"
echo "  • Homebrew 설치 시 sudo 비밀번호 입력"
echo ""
echo "전체 소요 시간: 10~20분 (인터넷 속도 + 큰 패키지: faster-whisper / mediapipe)"
echo ""
read -p "계속하려면 Enter, 취소하려면 Ctrl+C 를 누르세요... " _

# ---- 1. macOS / 아키텍처 확인 ---------------------------------------------
step "1. 시스템 점검"
ARCH=$(uname -m)
MACOS_VERSION=$(sw_vers -productVersion)
info "macOS: $MACOS_VERSION  /  아키텍처: $ARCH"

if [ "$ARCH" != "arm64" ]; then
  warn "Apple Silicon (M1/M2/M3/M4/M5) 이 아닙니다."
  warn "Intel Mac 도 동작은 하지만 영상 분석 (faster-whisper / mediapipe) 이 매우 느립니다."
  echo ""
  read -p "그래도 진행하시려면 Enter (취소: Ctrl+C)... " _
fi

# ---- 2. Xcode Command Line Tools ------------------------------------------
step "2. Xcode Command Line Tools"
if xcode-select -p &>/dev/null; then
  ok "이미 설치되어 있음"
else
  warn "Xcode Command Line Tools 가 없어 설치를 시작합니다."
  info "잠시 후 Apple 다이얼로그가 뜹니다 → '설치' 버튼을 클릭해주세요."
  xcode-select --install &>/dev/null || true
  info "설치 완료까지 대기 중..."
  while ! xcode-select -p &>/dev/null; do
    sleep 5
    echo -n "."
  done
  echo ""
  ok "Xcode Command Line Tools 설치 완료"
fi

# ---- 3. Homebrew ----------------------------------------------------------
step "3. Homebrew"
if ! command -v brew &>/dev/null && [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi

if ! command -v brew &>/dev/null; then
  warn "Homebrew 가 없어 설치를 시작합니다."
  info "도중에 sudo 비밀번호 입력 화면이 한 번 뜹니다."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    if ! grep -q '/opt/homebrew/bin/brew shellenv' "$HOME/.zprofile" 2>/dev/null; then
      echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
    fi
  fi
  ok "Homebrew 설치 완료"
else
  ok "이미 설치되어 있음"
fi

# ---- 4. Python 3.13 -------------------------------------------------------
step "4. Python 3.13"
if command -v "$PYTHON_BIN" &>/dev/null; then
  ok "이미 설치되어 있음 ($("$PYTHON_BIN" --version))"
else
  info "Python 3.13 을 brew 로 설치합니다..."
  brew install python@3.13
  ok "Python 3.13 설치 완료"
fi

# ---- 5. ffmpeg ------------------------------------------------------------
step "5. ffmpeg (yt-dlp · 영상 분석 · 자동편집 모두 의존)"
if command -v ffmpeg &>/dev/null; then
  ok "이미 설치되어 있음"
else
  info "ffmpeg 를 brew 로 설치합니다..."
  brew install ffmpeg
  ok "ffmpeg 설치 완료"
fi

# ---- 6. deno (yt-dlp 의 YouTube JS 챌린지 해결용) -------------------------
# showdon-downloader v0.4.4 fix — yt-dlp 가 YouTube cipher 복호화 (JS 챌린지) 에
# deno 사용. 없으면 다운로드 시 "Requested format is not available" 발생.
step "6. deno (YouTube JS 챌린지 해결)"
if command -v deno &>/dev/null; then
  ok "이미 설치되어 있음"
else
  info "deno 를 brew 로 설치합니다..."
  brew install deno
  ok "deno 설치 완료"
fi

# ---- 7. git ---------------------------------------------------------------
step "7. git"
if command -v git &>/dev/null; then
  ok "이미 설치되어 있음"
else
  info "git 을 brew 로 설치합니다..."
  brew install git
fi

# ---- 8. 저장소 클론 / 업데이트 --------------------------------------------
step "8. showdon-yejja 저장소"

if [ -d "$INSTALL_DIR/.git" ]; then
  info "최신 버전으로 업데이트 (git pull)..."
  cd "$INSTALL_DIR"
  git pull origin main || warn "git pull 실패 (오프라인/인증 이슈) — 기존 코드로 진행"
  ok "업데이트 완료"
elif [ -d "$INSTALL_DIR" ]; then
  err "$INSTALL_DIR 폴더가 존재하지만 git 저장소가 아닙니다."
  err "수동으로 폴더를 비우거나 다른 위치에 설치해주세요."
  exit 1
else
  info "$INSTALL_DIR 에 저장소를 새로 받습니다..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone "$REPO_URL" "$INSTALL_DIR"
  ok "Clone 완료"
fi

cd "$INSTALL_DIR"

# 작업 폴더 (~/showdon/yejjas) 자동 생성 — 코워크 프로젝트가 가리킬 폴더
mkdir -p "$HOME/showdon/yejjas"
ok "작업 폴더 준비: $HOME/showdon/yejjas"

# ---- 9. Python 가상환경 ---------------------------------------------------
step "9. Python 가상환경 (venv)"
if [ ! -d "venv" ]; then
  info "venv 생성 중..."
  "$PYTHON_BIN" -m venv venv
  ok "venv 생성 완료"
else
  ok "기존 venv 재사용"
fi

# shellcheck disable=SC1091
source venv/bin/activate

info "pip 업그레이드..."
pip install --upgrade pip --quiet

# ---- 10. 의존성 설치 ------------------------------------------------------
step "10. 의존성 설치 (PySide6 + faster-whisper + mediapipe + VectCutAPI deps 등)"
info "패키지 설치 중... (5~10분, 큰 패키지 다수 포함)"
pip install -r requirements.txt
ok "의존성 설치 완료"

# ---- 11. yt-dlp 최신 동기화 (git master) -----------------------------------
# showdon-downloader v0.4.5/0.4.7 fix — requirements.txt 의 yt-dlp>=X 만으론 기존
# 사용자 yt-dlp 가 upgrade 안 됨. YouTube 가 봇 감지/POT 정책을 자주 강화하므로
# install 마다 항상 git master (가장 최신 코드) 로 동기화. 모든 팀원 환경 통일.
step "11. yt-dlp 최신 동기화 (git master)"
info "yt-dlp 를 git master 로 업그레이드 중... (1~2분)"
pip install -U "yt-dlp @ git+https://github.com/yt-dlp/yt-dlp.git" --quiet
ok "yt-dlp 동기화 완료 ($(python -c 'import yt_dlp; print(yt_dlp.version.__version__)'))"

# ---- 12. 검증 -------------------------------------------------------------
step "12. 설치 검증"
python - <<'PYEOF'
import sys
print(f"Python: {sys.version.split()[0]}")
import PySide6
print(f"PySide6: {PySide6.__version__}")
import faster_whisper
print(f"faster-whisper: 설치됨")
import mediapipe
print(f"mediapipe: {mediapipe.__version__}")
import yt_dlp
print(f"yt-dlp: {yt_dlp.version.__version__}")
import flask, requests
print(f"flask: {flask.__version__} / requests: {requests.__version__}  (VectCutAPI 의존성)")
PYEOF
ok "검증 통과"

# ---- 13. 실행 권한 --------------------------------------------------------
chmod +x "$INSTALL_DIR/run.command" 2>/dev/null || true
chmod +x "$INSTALL_DIR/make_app.sh" 2>/dev/null || true

# ---- 14. .app 자동 빌드 ---------------------------------------------------
step "14. .app 번들 빌드"
deactivate 2>/dev/null || true
APP_BUILT=0
if (cd "$INSTALL_DIR" && ./make_app.sh); then
  ok ".app 빌드 완료 → $INSTALL_DIR/dist/쇼돈 예짜.app"
  APP_BUILT=1
else
  warn ".app 빌드 실패 — 나중에 수동 실행: cd $INSTALL_DIR && ./make_app.sh"
fi

# ---- 마무리 ---------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}================================================${NC}"
echo -e "${GREEN}${BOLD}          🎉  설치 모두 완료!${NC}"
echo -e "${GREEN}${BOLD}================================================${NC}"
echo ""
if [ "$APP_BUILT" = "1" ]; then
  echo "다음 파일을 더블클릭해 GUI 를 실행하세요:"
  echo ""
  echo -e "    ${BOLD}$INSTALL_DIR/dist/쇼돈 예짜.app${NC}"
  echo ""
  echo "  • 첫 실행만 우클릭 → '열기' (Gatekeeper 우회)"
  echo "  • Dock 에 끌어다 놓으면 한 번 클릭으로 실행"
else
  echo "다음 파일을 더블클릭해 GUI 를 실행하세요:"
  echo ""
  echo -e "    ${BOLD}$INSTALL_DIR/run.command${NC}"
fi
echo ""
echo -e "${YELLOW}${BOLD}>>> 코워크 셋업 (처음 한 번만)${NC}"
echo ""
echo "  1) Claude 데스크탑 앱 설치 + 로그인"
echo "  2) 새 코워크 프로젝트 생성 (이름 자유 — 예: '쇼돈 예짜')"
echo "  3) 작업 폴더로 ${BOLD}~/showdon/yejjas${NC} 선택"
echo "  4) System Prompt 에 다음 파일 통째로 붙여넣기:"
echo -e "       ${BOLD}$INSTALL_DIR/SYSTEM_PROMPT_v1.9.1.md${NC}"
echo ""
echo "     (업데이트 후엔 가장 최신 v1.x.x 파일로 갈아끼우세요)"
echo ""
echo "  자세한 설명은 README.md → '코워크 셋업' 섹션 참조."
echo ""
