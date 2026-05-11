#!/bin/bash
# =============================================================================
# 쇼돈 예짜.app 의 실행 진입점
# .app 더블클릭 시 macOS LaunchServices 가 이 스크립트를 호출.
# Terminal 창을 띄우지 않고 GUI 만 바로 실행.
# =============================================================================

INSTALL_DIR="$HOME/showdon/showdon-yejja"
PYTHON="$INSTALL_DIR/venv/bin/python"
LOG_FILE="/tmp/showdon-yejja-launcher.log"

# ★ v0.1.1 — PATH 보강. macOS LaunchServices 가 .app launch 시 PATH 를
# /usr/bin:/bin:/usr/sbin:/sbin 정도로 매우 제한적으로 주기 때문에
# brew 의 ffmpeg/ffprobe (/opt/homebrew/bin Apple Silicon, /usr/local/bin Intel)
# 가 안 보임 → 자동편집 모드에서 FileNotFoundError: 'ffmpeg'.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:$PATH"

# ★ v1.9.1 — .app 빌드 결과 = PROD 모드 강제.
# backend/edit.py 의 DEBUG_AUTO_OUTPUT_DIR 가 환경변수 보고 None 으로 분기.
# 즉 자동편집 결과물의 ~/showdon/yejjas_test/auto 자동 복사 OFF.
# 본인이 `python gui.py` 또는 `run.command` 로 직접 띄울 때는 env 없으니 디버그 ON.
# 디버그 .app 만들고 싶으면 빌드 후 이 줄을 .app/Contents/MacOS/launcher 에서 제거.
export YEJJA_PROD=1

# 로그를 파일로 (디버그용 — 문제 생기면 이 파일 확인)
exec > "$LOG_FILE" 2>&1
echo "[launcher] $(date '+%F %T') — 시작"
echo "[launcher] INSTALL_DIR=$INSTALL_DIR"
echo "[launcher] PYTHON=$PYTHON"
echo "[launcher] PATH=$PATH"
which ffmpeg 2>&1 | sed 's/^/[launcher] which ffmpeg: /'
which ffprobe 2>&1 | sed 's/^/[launcher] which ffprobe: /'
# ★ deno — yt-dlp 의 YouTube JS 챌린지 (cipher) 해결에 필수. 없으면 다운로드 시
# "Requested format is not available" 발생 (showdon-downloader v0.4.4 와 동일).
which deno 2>&1 | sed 's/^/[launcher] which deno: /'

# 1) 설치 여부 확인
if [ ! -x "$PYTHON" ]; then
    echo "[launcher] ERR: $PYTHON 가 실행 가능하지 않음 (venv 미설치 또는 권한 문제)"
    osascript <<EOF
display dialog "쇼돈 예짜가 설치되어 있지 않거나 접근 권한이 없습니다.

가능한 원인:
1) install.command 를 아직 실행하지 않음
2) macOS 가 ~/Documents 접근을 차단함

해결 방법은 README 의 'TCC 권한' 섹션 참고.

설치 위치: $INSTALL_DIR
로그 파일: $LOG_FILE" buttons {"확인"} default button 1 with icon caution with title "쇼돈 예짜"
EOF
    exit 1
fi

if [ ! -f "$INSTALL_DIR/gui.py" ]; then
    echo "[launcher] ERR: gui.py 가 없음"
    osascript -e 'display dialog "gui.py 를 찾을 수 없습니다.\n\nshowdon-yejja 폴더가 손상됐거나 이동된 것 같습니다." buttons {"확인"} default button 1 with icon stop with title "쇼돈 예짜"'
    exit 1
fi

# 2) venv 의 python 으로 GUI 직접 실행 (activate 스크립트 미사용 — 더 견고)
cd "$INSTALL_DIR"
echo "[launcher] $PYTHON gui.py 실행"
exec "$PYTHON" gui.py
