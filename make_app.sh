#!/bin/bash
# =============================================================================
# make_app.sh — '쇼돈 예짜.app' 번들 빌드 스크립트
#
# 사용:
#     ./make_app.sh
#
# 결과:
#     dist/쇼돈 예짜.app
#
# 동작:
#     1) design/icon.svg → design/icon.icns 변환 (PySide6 + iconutil)
#     2) .app 디렉토리 구조 생성:
#         쇼돈 예짜.app/
#         └── Contents/
#             ├── Info.plist          (메타데이터)
#             ├── MacOS/launcher      (실행 진입점)
#             └── Resources/icon.icns (Dock 아이콘)
#     3) launcher 에 실행 권한 부여
#     4) ad-hoc codesign (Apple Developer 계정 없이 TCC 다이얼로그 정상화)
#
# 빌드 후 결과 .app 을 ~/Applications 으로 옮기거나 Dock 에 드래그.
# =============================================================================

set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="쇼돈 예짜"
DIST_DIR="dist"
APP_DIR="${DIST_DIR}/${APP_NAME}.app"
CONTENTS="${APP_DIR}/Contents"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

step() { echo -e "${BLUE}[STEP]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
err()  { echo -e "${RED}[ERR]${NC}  $1"; }

# ---- 사전 검증 -----------------------------------------------------------
if [ ! -f "design/icon.svg" ]; then
    err "design/icon.svg 가 없습니다."
    exit 1
fi
if [ ! -f "design/Info.plist" ]; then
    err "design/Info.plist 가 없습니다."
    exit 1
fi
if [ ! -f "design/launcher.sh" ]; then
    err "design/launcher.sh 가 없습니다."
    exit 1
fi
if [ ! -f "design/make_icns.py" ]; then
    err "design/make_icns.py 가 없습니다."
    exit 1
fi
if [ ! -d "venv" ]; then
    err "venv 가 없습니다. 먼저 install.command 를 실행하거나 수동으로 설치해주세요."
    exit 1
fi

# ---- 1) icon.svg → icon.icns -------------------------------------------
step "1/4  icon.svg → icon.icns 변환"
# shellcheck disable=SC1091
source venv/bin/activate
python design/make_icns.py
if [ ! -f "design/icon.icns" ]; then
    err "icon.icns 생성 실패"
    exit 1
fi
ok "icon.icns 생성 완료"

# ---- 2) .app 디렉토리 구조 생성 ----------------------------------------
step "2/4  .app 번들 구조 생성"
rm -rf "${APP_DIR}"
mkdir -p "${CONTENTS}/MacOS" "${CONTENTS}/Resources"

cp design/Info.plist "${CONTENTS}/Info.plist"
cp design/icon.icns  "${CONTENTS}/Resources/icon.icns"
cp design/launcher.sh "${CONTENTS}/MacOS/launcher"
chmod +x "${CONTENTS}/MacOS/launcher"
ok ".app 구조 완료"

# ---- 3) ad-hoc 코드사인 -----------------------------------------------
# macOS 가 unsigned 앱의 TCC 권한 요청 다이얼로그를 띄우지 않고 silent 하게 거부하는 문제를 회피.
# ad-hoc 서명(--sign -)은 Apple Developer 계정 없이도 가능하며, TCC 다이얼로그를 정상 표시함.
step "3/4  ad-hoc 코드사인"
codesign --force --deep --sign - "${APP_DIR}" 2>&1 | sed 's/^/  /' || {
    err "codesign 실패 — Xcode CLI Tools 가 설치되어 있는지 확인하세요."
    exit 1
}
# 검증 (warnings 만 나오면 OK)
codesign --verify --verbose=2 "${APP_DIR}" 2>&1 | sed 's/^/  /' || true
ok "ad-hoc 서명 완료"

# ---- 4) 마무리 + Finder 에서 보여주기 ---------------------------------
step "4/4  Finder 에서 dist/ 폴더 열기"
open "${DIST_DIR}/" || true

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅  ${APP_NAME}.app 빌드 완료!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo ""
echo "결과물 위치:"
echo "  ${PWD}/${APP_DIR}"
echo ""
echo "다음 단계:"
echo "  • 더블클릭으로 바로 테스트 가능"
echo "  • Dock 에 끌어다 놓으면 영구 등록"
echo "  • ~/Applications/ 으로 옮기면 Spotlight 에서 검색 가능:"
echo "      mv \"${APP_DIR}\" ~/Applications/"
echo ""
echo "💡 첫 실행 시 macOS 가 '확인되지 않은 개발자' 경고를 띄울 수 있습니다."
echo "   → 우클릭 → '열기' → 다이얼로그에서 한 번 더 '열기' 클릭"
echo ""
