# -*- coding: utf-8 -*-
"""
backend/edit.py — 4단계 편집 코어.

편집점 폴더(edit_plan.json + dialog.srt + explain.srt) 받아 완성 mp4 생성.

흐름:
  1. SRT 의 [follow:N] 마커 strip → output_dir 에 복사
  2. PIL 1080×1920 흰 캔버스 + 채널 헤더 + 타이틀 + 출처 footer → template.png
  3. ffmpeg sub_cuts 추출 (각각 cuts/cutNN.mp4)
  4. ffmpeg concat → full_raw.mp4
  5. ffmpeg single-pass: 영상 letterbox + 흰 캔버스 패딩 + template overlay + 자막 burn → full.mp4
  6. produced meta.json + _DONE 마커

Renderer 추상화로 v1.1 CapCutDraftAdapter 추가 가능 (현재 stub).
heavy import (PIL) 는 lazy.
"""

from __future__ import annotations

import re
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING


# ★ v0.1.2 임시 디버그 모드 — 자동편집 결과물을 한 곳에 모아두기.
# - 값이 path 면 자동편집 (FfmpegRenderer) 결과 full.mp4 가 거기에 <폴더이름>.mp4
#   로 복사됨. 원본 (~/showdon/yejjas/<채널>/완성/<폴더>/full.mp4) 은 그대로.
# - CapCut 모드는 .mp4 결과 아니라서 자동 skip.
# - 끄려면 None 으로.
#
# ★ v1.9.3 — 우선순위 (높→낮):
#   1) 환경변수 YEJJA_PROD=1 → None (PROD 모드, 자동복사 OFF)
#   2) 환경변수 YEJJA_DEBUG_AUTO_DIR (수동 override) → 그 path
#   3) AppConfig.debug_auto_output_dir (디버깅 탭에서 GUI 로 변경 가능)
#   4) Default fallback: ~/showdon/yejjas_test/auto
#
# 사용처 (line 2150 근처) 가 모듈 import 시점이 아닌 함수 호출 시점에
# resolve 되도록 함수 호출. _resolve_debug_auto_output_dir() 매번 재평가.
_DEBUG_AUTO_OUTPUT_DIR_DEFAULT: Optional[str] = "~/showdon/yejjas_test/auto"


def _resolve_debug_auto_output_dir() -> Optional[str]:
    """우선순위 따라 디버그 자동출력 폴더 결정. 자동편집 호출 때마다 재평가.

    config 변경 즉시 반영 (GUI 재시작 없이) — 디버깅 탭에서 경로 변경 후 곧바로 효과.
    """
    # 1) PROD 모드 — 무조건 OFF
    if os.environ.get("YEJJA_PROD") == "1":
        return None
    # 2) env 수동 override
    env_path = os.environ.get("YEJJA_DEBUG_AUTO_DIR")
    if env_path:
        return env_path
    # 3) AppConfig 에서 읽기 (디버깅 탭이 변경한 값)
    try:
        from .config import load_config
        cfg = load_config()
        if cfg.debug_auto_output_dir:
            return cfg.debug_auto_output_dir
    except Exception:
        pass  # 초기 import / 순환 import 방지
    # 4) Default
    return _DEBUG_AUTO_OUTPUT_DIR_DEFAULT


# ★ Legacy 호환 — 모듈 변수로도 노출 (테스트·외부 모니터링용).
# 단 실제 사용처는 _resolve_debug_auto_output_dir() 함수 호출 권장.
DEBUG_AUTO_OUTPUT_DIR: Optional[str] = _resolve_debug_auto_output_dir()


@lru_cache(maxsize=8)
def _resolve_bin(name: str) -> str:
    """ffmpeg / ffprobe 등 외부 바이너리 절대경로 찾기.

    ★ v0.1.1 — macOS .app launch 시 PATH 가 매우 제한적이라 brew 의 binary
    (/opt/homebrew/bin, /usr/local/bin) 못 찾는 케이스 대응. shutil.which 가
    실패하면 brew 표준 위치 fallback. 모든 fallback 도 실패하면 name 그대로
    리턴 (어차피 PATH 의존이라 거기서 FileNotFoundError 발생 — 디버깅 신호).
    """
    found = shutil.which(name)
    if found:
        return found
    for p in (
        f"/opt/homebrew/bin/{name}",
        f"/opt/homebrew/sbin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/local/sbin/{name}",
    ):
        if Path(p).exists():
            return p
    return name

from .schema import (
    MARKER_DONE, MARKER_FAILED,
    AppConfig, ChannelConfig,
    EditPlan, EditPlanSubCut, FocusBox,
    FolderMeta, FolderMetaKind,
    SubtitleStyle,
    _default_dialog_style, _default_explain_style,
    load_dataclass, save_dataclass, write_marker,
)


# =============================================================================
# 진행 이벤트 / 예외
# =============================================================================

@dataclass
class EditProgressEvent:
    step: str                       # font | template | subcut | concat | composite | done
    percent: float = 0.0
    message: str = ""
    elapsed_s: float = 0.0


ProgressCb = Callable[[EditProgressEvent], None]
CancelCb = Callable[[], bool]


class EditError(RuntimeError):
    """편집 실패 (ffmpeg·폰트·소스영상 등)."""


class EditCancelled(Exception):
    """사용자 취소."""


# =============================================================================
# 폰트 자동 다운로드
# =============================================================================

NOTO_SANS_KR_BOLD_URL = (
    "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/Korean/NotoSansCJKkr-Bold.otf"
)

# ★ 비-BMP 이모지 (🎶🐶🔥 등) fallback — Noto Sans CJK KR 에 비-BMP pictographs
# 글리프 없어서 libass 가 tofu (□) 렌더링하는 문제 fix.
#
# Noto Emoji (monochrome variable, ~2MB, Apache 2.0). family = "Noto Emoji".
# ensure_font 가 자동 다운로드 → ~/Library/Fonts/ 등록 → libass 가 fontconfig 로 매칭.
#
# ★ 학습사항 (2026-05-12, M4 Pro / Homebrew ffmpeg):
# 컬러 이모지 시도 2회 모두 tofu — Apple Color Emoji (.ttc + CBDT) 도, Noto Color
# Emoji (.ttf + CBDT) 도 libass 가 풀지 못함. fontconfig 자동 fallback substitution
# 도 호출 안 됨. 즉 사용자 ffmpeg 빌드는 **CBDT color bitmap 자체** 호환 X.
# 흑백 monochrome (Noto Emoji glyf 테이블) 만 안정. 향후 ffmpeg 빌드 업데이트되면 재시도 가능.
NOTO_EMOJI_URL = (
    "https://github.com/google/fonts/raw/main/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf"
)
NOTO_EMOJI_FILENAME = "NotoEmoji-VariableFont_wght.ttf"
NOTO_EMOJI_FAMILY = "Noto Emoji"

# ASS 안 비-BMP 이모지 wrap 시 사용할 폰트 family. 흑백 monochrome 만 안정.
EMOJI_RENDER_FAMILY = NOTO_EMOJI_FAMILY


def ensure_font(font_path: Path) -> Path:
    """주 폰트 (Noto Sans CJK KR Bold) + 이모지 폰트 (Noto Emoji) 자동 다운로드.

    macOS 에서 ~/Library/Fonts/ 의 폰트는 시스템 fontconfig 에 자동 등록됨.
    ffmpeg/libass 가 ASS 의 Fontname 으로 자동 매칭 → fontsdir 옵션 불필요.

    Args:
        font_path: 주 폰트 (CJK KR) 의 캐시 경로. 이모지 폰트는 같은 디렉토리에
            ``NotoEmoji-VariableFont_wght.ttf`` 로 별도 저장 — _srt_to_ass 의
            ``{\\fnNoto Emoji}`` inline 태그 fallback 용.
    """
    font_path = Path(font_path).expanduser()

    # 1. 주 폰트 (Noto Sans CJK KR Bold) 다운로드
    if not font_path.exists():
        font_path.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["curl", "-fsSL", NOTO_SANS_KR_BOLD_URL, "-o", str(font_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not font_path.exists():
            raise EditError(f"폰트 다운로드 실패: {r.stderr.strip()[:200]}")

    # 2. 이모지 폰트 (Noto Emoji 흑백) 다운로드 — 실패해도 주 폰트는 동작 (best-effort).
    # 이모지 없는 영상은 정상 동작, 이모지 있는 영상만 tofu 영향.
    emoji_path = font_path.parent / NOTO_EMOJI_FILENAME
    if not emoji_path.exists():
        r = subprocess.run(
            ["curl", "-fsSL", NOTO_EMOJI_URL, "-o", str(emoji_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not emoji_path.exists():
            # 불완전 파일 있으면 삭제 (다음 호출 때 재시도)
            try:
                if emoji_path.exists():
                    emoji_path.unlink()
            except Exception:
                pass

    # 3. macOS 시스템 폰트 폴더에 복사 (libass 가 fontconfig 로 찾을 수 있게)
    try:
        user_fonts_dir = Path.home() / "Library" / "Fonts"
        user_fonts_dir.mkdir(parents=True, exist_ok=True)
        for src in (font_path, emoji_path):
            if not src.exists():
                continue
            target = user_fonts_dir / src.name
            if not target.exists():
                shutil.copy2(src, target)
    except Exception:
        # 시스템 등록 실패해도 fallback 폰트로 표시는 됨
        pass

    return font_path


# 비-BMP 코드포인트 감지 — 🐶🎶🔥 등 pictographs/emoji (Noto Sans CJK KR 미커버 영역).
# BMP 안 ♪♫★♥ 같은 BMP symbol 은 Noto Sans CJK KR 가 자체 커버하므로 wrap X.
_EMOJI_RE = re.compile(r'[\U00010000-\U0010FFFF]+')


def _inject_emoji_fontname(text: str, main_font: str,
                           emoji_font: str = EMOJI_RENDER_FAMILY) -> str:
    """ASS 텍스트의 비-BMP 이모지를 ``{\\fn<emoji>}…{\\fn<main>}`` 인라인 태그로 wrap.

    Why: Noto Sans CJK KR 는 비-BMP pictographs (🎶🐶🔥 U+1F000+) 글리프 미보유 →
    libass 가 tofu (□) 로 렌더. fontconfig 자동 fallback 은 ffmpeg/libass 빌드별
    동작 다름 → inline ``{\\fn}`` 으로 명시 전환이 가장 견고.

    ASS override block ({\\an8}, {\\pos(x,y)} 등) 은 그대로 보존.
    """
    if not _EMOJI_RE.search(text):
        return text

    # {tag} 와 일반 텍스트 분리 — re.split(keep delimiters)
    parts = re.split(r'(\{[^}]*\})', text)
    out: list[str] = []
    for p in parts:
        if p.startswith('{') and p.endswith('}'):
            out.append(p)  # override block — 그대로
        else:
            # 일반 텍스트 — 비-BMP 만 emoji 폰트로 wrap
            out.append(_EMOJI_RE.sub(
                lambda m: f'{{\\fn{emoji_font}}}{m.group(0)}{{\\fn{main_font}}}',
                p,
            ))
    return ''.join(out)


# =============================================================================
# Subtitle preprocessing — [follow:N] 마커 처리 (v1.3 활성화)
# =============================================================================

_FOLLOW_MARKER = re.compile(
    r"\s*\[follow:(\d+)(?:\s+offset:([^\]]*))?\]\s*"
)


def strip_follow_markers(srt_in: Path, srt_out: Path) -> int:
    """SRT 의 `[follow:N offset:...]` 마커를 단순 제거 (v1.0/1.1 fallback).

    v1.3+ 는 expand_follow_markers 사용 권장. face_clusters 미존재 시 fallback.

    Returns:
        제거된 마커 수
    """
    text = srt_in.read_text(encoding="utf-8")
    cleaned, count = re.subn(r"\s*\[follow:[^\]]*\]\s*", " ", text)
    cleaned = re.sub(r"  +", " ", cleaned)
    srt_out.parent.mkdir(parents=True, exist_ok=True)
    srt_out.write_text(cleaned, encoding="utf-8")
    return count


def _parse_offset(offset_str: str) -> tuple[int, int]:
    """[follow:N offset:x=10 y=-40] 의 offset 파싱."""
    dx = dy = 0
    for tok in (offset_str or "").split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        try:
            iv = int(v)
        except ValueError:
            continue
        if k.strip() == "x":
            dx = iv
        elif k.strip() == "y":
            dy = iv
    return dx, dy


def expand_follow_markers(
    srt_in: Path,
    srt_out: Path,
    face_clusters: dict | None,
    *,
    sub_cut_offset_s: float = 0.0,
    canvas_w: int | None = None,
    video_zone_x: int | None = None,
    video_zone_y: int | None = None,
    video_zone_w: int | None = None,
    video_zone_h: int | None = None,
) -> int:
    """v1.3 — [follow:N] 마커 → ASS \\pos() 인라인 태그로 변환.

    face_clusters.frames[].t (절대 시각) 와 SRT 시각 (상대) 매칭:
      - SRT 자막의 시작 시각 + sub_cut_offset_s = 절대 시각
      - 그 시각에 cluster_id N 의 face bbox 평균 위치 사용
      - 영상 좌표 → 캔버스 좌표 변환 (영상 박스 zone 안)
      - {\\an2\\pos(cx, cy+offset)} 박음 (얼굴 아래쪽 자연스러운 위치)

    face_clusters 가 None 이거나 cluster_id 매칭 실패면 마커만 strip (fallback).

    Returns:
        expand 된 마커 수.
    """
    # 캔버스 상수 fallback (모듈 상단 상수 — 함수 정의 시점에 안 보여서 default 못 쓸 때 대비)
    if canvas_w is None: canvas_w = CANVAS_W
    if video_zone_x is None: video_zone_x = VIDEO_ZONE_X
    if video_zone_y is None: video_zone_y = VIDEO_ZONE_Y
    if video_zone_w is None: video_zone_w = VIDEO_ZONE_W
    if video_zone_h is None: video_zone_h = VIDEO_ZONE_H

    text = srt_in.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", text.strip())

    # 클러스터별 frame 인덱스 (cluster_id → [(t_abs, bbox_norm_center_xy), ...])
    cluster_pos: dict[int, list[tuple[float, float, float]]] = {}
    if face_clusters:
        for fr in face_clusters.get("frames", []):
            t = float(fr.get("t", 0.0))
            for face in fr.get("faces", []):
                cid = face.get("cluster_id")
                if cid is None:
                    continue
                bb = face.get("bbox_norm", {})
                cx = float(bb.get("x", 0.0)) + float(bb.get("w", 0.0)) / 2.0
                cy = float(bb.get("y", 0.0)) + float(bb.get("h", 0.0)) / 2.0
                cluster_pos.setdefault(int(cid), []).append((t, cx, cy))

    def find_face_pos(cid: int, t_rel: float) -> tuple[float, float] | None:
        """cluster cid 의 t_rel (sub_cut 상대) 시각에 가까운 얼굴 위치 (정규화 cx, cy)."""
        t_abs = t_rel + sub_cut_offset_s
        seq = cluster_pos.get(cid)
        if not seq:
            return None
        # 절대 시각 차이 최소
        best = min(seq, key=lambda r: abs(r[0] - t_abs))
        if abs(best[0] - t_abs) > 2.0:  # 2초 이내 매칭만 유효
            return None
        return best[1], best[2]

    out_blocks: list[str] = []
    expanded = 0
    ts_re = re.compile(
        r"(\d+):(\d+):(\d+)[,\.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,\.](\d+)"
    )

    def srt_ts_to_seconds(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms[:3].ljust(3, "0")) / 1000.0

    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        # 첫 줄 인덱스 또는 timestamp
        new_lines = list(lines)
        # 자막 시작 시각 추출 (block 의 ts line)
        t_start: float | None = None
        for ln in lines:
            m = ts_re.search(ln)
            if m:
                t_start = srt_ts_to_seconds(*m.groups()[:4])
                break

        # 텍스트 라인 (마커 포함) 찾아서 변환
        for i, ln in enumerate(new_lines):
            if "[follow:" not in ln:
                continue
            mk = _FOLLOW_MARKER.search(ln)
            if not mk:
                continue
            cid = int(mk.group(1))
            dx, dy = _parse_offset(mk.group(2) or "")
            # cluster 위치 검색
            pos = find_face_pos(cid, t_start or 0.0) if t_start is not None else None
            stripped_text = _FOLLOW_MARKER.sub(" ", ln).strip()
            stripped_text = re.sub(r"  +", " ", stripped_text)
            if pos is None:
                # fallback — 마커만 제거
                new_lines[i] = stripped_text
                continue
            cx_norm, cy_norm = pos
            # 영상 정규화 → 영상 zone 픽셀 → 캔버스 픽셀
            face_x = video_zone_x + cx_norm * video_zone_w
            face_y = video_zone_y + cy_norm * video_zone_h
            # 자막을 얼굴 아래쪽 60px (가독성), offset 추가
            sub_x = int(face_x + dx)
            sub_y = int(face_y + 60 + dy)
            # 캔버스 경계 안으로
            sub_x = max(40, min(canvas_w - 40, sub_x))
            sub_y = max(VIDEO_ZONE_Y + 40, min(SUBTITLE_AREA_Y - 40, sub_y))
            new_lines[i] = f"{{\\an2\\pos({sub_x},{sub_y})}}{stripped_text}"
            expanded += 1
        out_blocks.append("\n".join(new_lines))

    srt_out.parent.mkdir(parents=True, exist_ok=True)
    srt_out.write_text("\n\n".join(out_blocks) + "\n", encoding="utf-8")
    return expanded


# =============================================================================
# PIL 템플릿 합성 — 1080×1920 ★ v1.9.2 검정 캔버스 (영상 zone 투명, 1:1 정사각형)
# =============================================================================

# 캔버스 레이아웃 v1.2 + ★ v1.9.2 색감 변경
#
#   ┌──────────────────────────┐
#   │  채널 헤더 + 타이틀 (320) │   y = 0   ~ 320  ★ 검정 배경 + 흰글씨
#   ├──────────────────────────┤
#   │                          │
#   │  영상 박스 1:1 (1080)    │   y = 320 ~ 1400  (영상 zone, 투명)
#   │  (320 ~ 1400)            │
#   │                          │
#   ├──────────────────────────┤
#   │  자막 영역 (440)          │   y = 1400 ~ 1840  ★ v1.9.2 흰 → 검정
#   │  (대사 자막 burn — 흰글씨) │
#   ├──────────────────────────┤
#   │  출처 footer (80)        │   y = 1840 ~ 1920  ★ 검정 배경 + 밝은 회색 글씨
#   └──────────────────────────┘
CANVAS_W = 1080
CANVAS_H = 1920
HEADER_H = 320              # 0 ~ 320: 채널 헤더 + 타이틀
VIDEO_ZONE_X = 0
VIDEO_ZONE_Y = 320
VIDEO_ZONE_W = 1080
VIDEO_ZONE_H = 1080         # 320 ~ 1400: 1:1 영상 박스
SUBTITLE_AREA_Y = 1400
SUBTITLE_AREA_H = 440       # 1400 ~ 1840: 흰 자막 영역
FOOTER_Y = 1840             # 1840 ~ 1920: 출처

# Sub-cut 추출 시 영상 1:1 사이즈 (scale 후)
CUT_SQUARE_SIZE = 1080


def render_template_png(
    plan: EditPlan,
    channel: ChannelConfig,
    out_path: Path,
    *,
    font_path: Optional[Path] = None,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
    video_zone_x: int = VIDEO_ZONE_X,
    video_zone_y: int = VIDEO_ZONE_Y,
    video_zone_w: int = VIDEO_ZONE_W,
    video_zone_h: int = VIDEO_ZONE_H,
) -> tuple[Path, tuple[int, int, int, int]]:
    """1080×1920 RGBA 템플릿 PNG 생성. 영상 zone 은 alpha=0.

    v1.2: 영상 zone 1:1 (1080×1080), 헤더 320px, 자막영역 1400~1840, 출처 1840~.
    자막은 ASS burn 으로 후처리 — 여기선 안 그림.

    Returns:
        (template_png_path, (x, y, w, h))  영상 zone 위치/크기
    """
    from PIL import Image, ImageDraw, ImageFont  # lazy

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ★ v1.9.2 — 캔버스 흰 → 검정 배경. 영상 zone 만 투명 (1:1).
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 255))
    # 영상 zone 투명 (1:1)
    img.paste((0, 0, 0, 0),
              (video_zone_x, video_zone_y,
               video_zone_x + video_zone_w, video_zone_y + video_zone_h))

    draw = ImageDraw.Draw(img)

    # 폰트 로드 — v1.2 헤더 320px 안에 들어가게 사이즈 압축
    title_font = body_font = small_font = footer_font = None
    if font_path and Path(font_path).exists():
        try:
            title_font = ImageFont.truetype(str(font_path), 64)
            body_font = ImageFont.truetype(str(font_path), 30)
            small_font = ImageFont.truetype(str(font_path), 24)
            footer_font = ImageFont.truetype(str(font_path), 32)
        except Exception:
            pass
    if title_font is None:
        # PIL default — 한글 안 보이지만 fallback
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()
        small_font = ImageFont.load_default()
        footer_font = ImageFont.load_default()

    # === 채널 헤더 === (좌상단 — 슬림하게)
    icon_size = 76
    header_y = 24
    icon_x = 40
    text_x = icon_x + icon_size + 18

    if channel.icon_path:
        icon_p_str = channel.icon_path
        # {data_root} 치환은 caller 책임. 여기선 expanduser 만.
        icon_p = Path(icon_p_str).expanduser()
        if icon_p.exists():
            try:
                icon = Image.open(icon_p).convert("RGBA").resize((icon_size, icon_size))
                img.paste(icon, (icon_x, header_y), icon)
            except Exception:
                pass

    # ★ v1.9.2 — 검정 배경에 맞춰 텍스트 색감 변경 (어두운 → 밝은)
    if channel.handle:
        draw.text((text_x, header_y + 4), channel.handle,
                  fill=(240, 240, 240, 255), font=body_font)
    if channel.name_kr:
        draw.text((text_x, header_y + 42), channel.name_kr,
                  fill=(180, 180, 180, 255), font=small_font)

    # === 타이틀 === (multi-line, 가운데 정렬)
    # 헤더 0~120 사용했으니 타이틀은 120~ 시작, 320 안에 끝나야 함.
    # 64pt + line_h=78 → 두 줄이면 156px 차지 (120~276) ✓
    title_text = plan.template.title_text or (
        plan.titles[0].text if plan.titles else ""
    )
    if title_text:
        title_y = 130
        line_h = 78
        for line in title_text.split("\n"):
            line = line.strip()
            if not line:
                title_y += line_h
                continue
            try:
                bbox = draw.textbbox((0, 0), line, font=title_font)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = len(line) * 32
            x = max(20, (canvas_w - tw) // 2)
            # ★ v1.9.2 — 검정 배경 → 흰 글씨
            draw.text((x, title_y), line, fill=(255, 255, 255, 255), font=title_font)
            title_y += line_h

    # === 출처 footer === (가운데)
    footer_text = plan.footer.source_text or ""
    if footer_text:
        try:
            bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(footer_text) * 16
        x = max(20, (canvas_w - tw) // 2)
        # ★ v1.9.2 — 검정 배경 → 밝은 회색 글씨
        draw.text((x, FOOTER_Y + 16), footer_text,
                  fill=(200, 200, 200, 255), font=footer_font)

    img.save(out_path, "PNG")
    return out_path, (video_zone_x, video_zone_y, video_zone_w, video_zone_h)


# =============================================================================
# Sub-cut 추출 / Concat
# =============================================================================

def _adjust_focus_box_wide_shot(
    focus_box: Optional[FocusBox],
    sub_cut_start: float,
    sub_cut_end: float,
    face_clusters: dict | None,
    *,
    wide_face_count: int = 3,
    wide_ratio_threshold: float = 0.5,
    force_hide_caption: bool = True,
) -> Optional[FocusBox]:
    """v1.4.1 ★ 강화 — focus_box 의 hide_caption 강제 적용.

    force_hide_caption=True 면 face count 와 무관하게 모든 sub_cut 에 적용:
      - 코워크가 명시적으로 reason='hide_caption' 박은 경우 그대로 유지
      - 그 외 모든 케이스에서 h ≤ 0.85, y ≤ 0.05 로 clamp (원본 하단 자막 회피)
      - 클로즈업도 적용 — 화자 머리/턱 살짝 잘릴 수 있지만 원본 자막 노출보단 나음

    force_hide_caption=False 면 기존 v1.4 동작 (face count >= 3 wide shot 만 보정).
    """
    # v1.4.1 — 모든 sub_cut 에 hide_caption 강제
    if force_hide_caption:
        if focus_box is None:
            return FocusBox(x=0.0, y=0.0, w=1.0, h=0.85,
                            reason="hide_caption_auto")
        # 코워크가 명시적으로 hide_caption reason 박았으면 그대로
        if focus_box.reason and "hide_caption" in focus_box.reason:
            # 다만 h > 0.85 면 강제로 clamp
            if focus_box.h > 0.85:
                return FocusBox(
                    x=focus_box.x, y=0.0, w=focus_box.w, h=0.85,
                    reason=focus_box.reason,
                )
            return focus_box
        # 그 외 — h 강제 clamp + y=0
        new_h = min(focus_box.h, 0.85)
        return FocusBox(
            x=focus_box.x,
            y=0.0,
            w=focus_box.w,
            h=new_h,
            reason=f"hide_caption_auto({focus_box.reason or 'unset'})",
        )

    # legacy v1.4 동작 (face count 기반)
    if not face_clusters:
        return focus_box
    frames = face_clusters.get("frames", [])
    if not frames:
        return focus_box

    in_window = [
        fr for fr in frames
        if sub_cut_start <= float(fr.get("t", 0)) <= sub_cut_end
    ]
    if not in_window:
        return focus_box

    wide_count = sum(
        1 for fr in in_window
        if len(fr.get("faces", [])) >= wide_face_count
    )
    if wide_count / len(in_window) < wide_ratio_threshold:
        return focus_box

    if focus_box is None:
        return FocusBox(x=0.0, y=0.0, w=1.0, h=0.85,
                        reason="wide_shot_hide_caption_auto")
    new_h = min(focus_box.h, 0.85)
    return FocusBox(
        x=focus_box.x,
        y=0.0,
        w=focus_box.w,
        h=new_h,
        reason=f"wide_shot_hide_caption_auto({focus_box.reason or 'unset'})",
    )


def _build_crop_filter(focus_box: Optional[FocusBox]) -> str:
    """sub-cut 추출 시 1:1 정사각형 영역 crop filter 빌드.

    - focus_box 있으면 정규화 좌표(0~1) 그대로 사용
    - 없으면 폴백: min(iw,ih) 정사각형 + 가로 영상은 가운데 / 세로 영상은 위쪽
      위쪽 crop = 16:9 영상의 원본 자막 (보통 아래) 자동 제거 효과.
    """
    if focus_box:
        # 정규화 → 절대 px (iw, ih 사용). 코워크가 대략 1:1 비율로 잡아주면 자연스러움.
        # 비율이 다르면 다음 scale 단계에서 늘어남 — 일단 그대로 받음.
        return (
            f"crop=iw*{focus_box.w:.4f}:ih*{focus_box.h:.4f}:"
            f"iw*{focus_box.x:.4f}:ih*{focus_box.y:.4f}"
        )
    # 폴백: 1:1 정사각형, 위쪽 (가로 영상의 하단 자막 자동 제거)
    return (
        "crop="
        "'min(iw\\,ih)':"            # width = min(iw, ih)
        "'min(iw\\,ih)':"            # height = min(iw, ih)
        "'(iw-min(iw\\,ih))/2':"     # x = horizontal center
        "0"                           # y = 0 (top-aligned, 하단 자막 잘림)
    )


def _detect_black_segments(
    video: Path, *, threshold: float = 0.10, min_duration: float = 0.15,
) -> list[tuple[float, float]]:
    """ffmpeg blackdetect 로 검정 구간 검출.

    Returns:
        [(black_start, black_end), ...] 초 단위 리스트.
    """
    cmd = [
        _resolve_bin("ffmpeg"), "-hide_banner", "-i", str(video),
        "-vf", f"blackdetect=d={min_duration}:pix_th={threshold}",
        "-an", "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    segs: list[tuple[float, float]] = []
    # stderr 형식: "[blackdetect @ ...] black_start:1.234 black_end:2.345 black_duration:1.111"
    pattern = re.compile(r"black_start:(\d+\.?\d*)\s+black_end:(\d+\.?\d*)")
    for m in pattern.finditer(r.stderr or ""):
        segs.append((float(m.group(1)), float(m.group(2))))
    return segs


def _detect_dark_frames_luma(
    video: Path, *, luma_threshold: float = 30.0, sample_fps: float = 5.0,
) -> list[tuple[float, float]]:
    """signalstats 로 frame 평균 luma 검출 → 어두운 frame 구간 (★ v1.4 NEW).

    blackdetect 가 못 잡는 케이스 (검정 + 큰 흰 텍스트 = 챕터 자막) 도 잡음.
    sample_fps 로 다운샘플링해서 빠르게 (24fps 전부 검사 X).

    Returns:
        [(dark_start, dark_end), ...] 초 단위. 인접 dark frame 들 합쳐서 구간으로.
    """
    # signalstats 의 YAVG (Y 평균) 출력. select=fps=N 으로 다운샘플.
    cmd = [
        _resolve_bin("ffmpeg"), "-hide_banner", "-i", str(video),
        "-vf", f"fps={sample_fps},signalstats,metadata=print:key=lavfi.signalstats.YAVG",
        "-an", "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)

    # stderr 형식: "frame:N pts:T pts_time:T.TTT" 다음 줄에
    # "lavfi.signalstats.YAVG=N.NNNN"
    dark_times: list[float] = []
    pts_pattern = re.compile(r"pts_time:(\d+\.?\d*)")
    yavg_pattern = re.compile(r"lavfi\.signalstats\.YAVG=(\d+\.?\d*)")

    last_pts: float | None = None
    for line in (r.stderr or "").splitlines():
        m_pts = pts_pattern.search(line)
        if m_pts:
            last_pts = float(m_pts.group(1))
            continue
        m_yavg = yavg_pattern.search(line)
        if m_yavg and last_pts is not None:
            yavg = float(m_yavg.group(1))
            if yavg < luma_threshold:
                dark_times.append(last_pts)
            last_pts = None

    if not dark_times:
        return []

    # 인접 (1/sample_fps 이내) 구간 합치기
    gap = 1.5 / sample_fps
    segs: list[tuple[float, float]] = []
    seg_start = dark_times[0]
    seg_end = dark_times[0]
    for t in dark_times[1:]:
        if t - seg_end <= gap:
            seg_end = t
        else:
            segs.append((seg_start, seg_end + 1.0 / sample_fps))
            seg_start = t
            seg_end = t
    segs.append((seg_start, seg_end + 1.0 / sample_fps))
    return segs


def _trim_black_edges(
    cut_path: Path, *, encoding: dict, max_trim: float = 1.5,
) -> None:
    """sub-cut 의 시작·끝 max_trim 초 안에 있는 검정 구간 자동 trim (in-place).

    원본 영상 챕터 인터스티셜 (예: "다음 질문" 검정 화면) 이 sub_cut 경계에 끼면 제거.
    """
    # 영상 duration
    r = subprocess.run(
        [_resolve_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(cut_path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(r.stdout.strip())
    except (ValueError, AttributeError):
        return  # duration 못 얻으면 trim 안 함

    # v1.4 — blackdetect (완전 검정) + signalstats (검정+텍스트) 둘 다 사용
    black_segs = _detect_black_segments(cut_path)
    dark_segs = _detect_dark_frames_luma(cut_path)
    segs = sorted(set(black_segs + dark_segs), key=lambda s: s[0])
    if not segs:
        return

    # 시작 max_trim 초 안 검정: trim_start = black_end (or 0)
    trim_start = 0.0
    for bs, be in segs:
        if bs <= 0.2 and be < max_trim:
            trim_start = max(trim_start, be)

    # 끝 max_trim 초 안 검정: trim_end = duration - (duration - black_start)
    trim_end = duration
    for bs, be in segs:
        if be >= duration - 0.2 and (duration - bs) < max_trim:
            trim_end = min(trim_end, bs)

    if trim_start <= 0.05 and (duration - trim_end) <= 0.05:
        return  # trim 할 게 없음

    # 새 영상으로 복사
    tmp = cut_path.with_suffix(".trim.mp4")
    cmd = [
        _resolve_bin("ffmpeg"), "-y",
        "-ss", f"{trim_start:.3f}", "-to", f"{trim_end:.3f}",
        "-i", str(cut_path),
        "-c:v", encoding.get("video_codec", "libx264"),
        "-preset", encoding.get("preset", "medium"),
        "-crf", str(encoding.get("crf", 18)),
        "-c:a", encoding.get("audio_codec", "aac"),
        "-b:a", encoding.get("audio_bitrate", "192k"),
        "-pix_fmt", encoding.get("pix_fmt", "yuv420p"),
        str(tmp),
        "-loglevel", "error",
    ]
    rt = subprocess.run(cmd, capture_output=True, text=True)
    if rt.returncode == 0 and tmp.exists():
        tmp.replace(cut_path)
    else:
        try:
            tmp.unlink()
        except Exception:
            pass


def _ffmpeg_extract_subcut(
    video: Path,
    start_s: float,
    end_s: float,
    out_path: Path,
    *,
    encoding: dict,
    focus_box: Optional[FocusBox] = None,
    target_size: int = CUT_SQUARE_SIZE,
    trim_black: bool = True,
    apply_focus_box: bool = True,
    is_cancelled: CancelCb | None = None,
) -> None:
    """원본 영상의 [start_s, end_s] 구간을 1:1 정사각형 mp4 로 추출.

    v1.2: focus_box 가 있으면 그 영역으로 crop, 없으면 폴백 (가로 영상 → 1:1 위쪽).
    그 후 target_size × target_size 로 scale (lanczos).
    """
    if is_cancelled and is_cancelled():
        raise EditCancelled()

    # v1.6 — apply_focus_box=False (capcut export 모드) 면 영상 원본 비율 유지
    if apply_focus_box:
        crop = _build_crop_filter(focus_box)
        # focus_box 비율이 1:1 아니어도 stretch 방지.
        # increase + crop = 비율 유지하며 정사각형 채움 (가장자리만 살짝 잘림, letterbox X)
        vf = (
            f"{crop},"
            f"scale={target_size}:{target_size}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={target_size}:{target_size},"
            f"setsar=1"
        )
    else:
        # capcut export 모드 — 영상 원본 비율 그대로 (시간만 자름)
        vf = "setsar=1"

    cmd = [
        _resolve_bin("ffmpeg"), "-y",
        "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
        "-i", str(video),
        "-vf", vf,
        "-c:v", encoding.get("video_codec", "libx264"),
        "-preset", encoding.get("preset", "medium"),
        "-crf", str(encoding.get("crf", 18)),
        "-c:a", encoding.get("audio_codec", "aac"),
        "-b:a", encoding.get("audio_bitrate", "192k"),
        "-pix_fmt", encoding.get("pix_fmt", "yuv420p"),
        str(out_path),
        "-loglevel", "error",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise EditError(
            f"sub-cut 추출 실패 ({start_s:.1f}~{end_s:.1f}): "
            f"{r.stderr.strip()[:300]}"
        )

    # v1.3 — 시작·끝 검정 frame 자동 trim (원본 챕터 인터스티셜 회피)
    if trim_black:
        try:
            _trim_black_edges(out_path, encoding=encoding)
        except Exception:
            pass  # trim 실패해도 영상은 살리기


def _ffmpeg_concat(cut_paths: list[Path], out_path: Path) -> None:
    """concat list 사용해 합치기. -c copy 시도, 실패 시 재인코딩 fallback.

    concat list 는 /tmp 사용 (한글 경로·권한 이슈 회피).
    """
    fd, list_file = tempfile.mkstemp(suffix=".txt", dir="/tmp")
    try:
        with open(fd, "w") as f:
            for p in cut_paths:
                # ffmpeg concat list 의 'file' 라인은 single-quote escape 필요
                escaped = str(p.absolute()).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # -c copy 시도
        cmd_copy = [
            _resolve_bin("ffmpeg"), "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            str(out_path),
            "-loglevel", "error",
        ]
        r = subprocess.run(cmd_copy, capture_output=True, text=True)
        if r.returncode == 0:
            return

        # 재인코딩 fallback
        cmd_reenc = [
            _resolve_bin("ffmpeg"), "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            str(out_path),
            "-loglevel", "error",
        ]
        r2 = subprocess.run(cmd_reenc, capture_output=True, text=True)
        if r2.returncode != 0:
            raise EditError(f"concat 실패: {r2.stderr.strip()[:300]}")
    finally:
        try:
            Path(list_file).unlink()
        except Exception:
            pass


# =============================================================================
# Single-pass composite — 영상 letterbox + template overlay + 자막 burn
# =============================================================================

def _ass_color(srt_color: str) -> str:
    """이미 ASS 형식이면 그대로. schema 는 '&HBBGGRR&' 형식으로 받기로 합의."""
    return srt_color


# ASS 의 PlayResX/Y — v1.2 부터 캔버스 (1080×1920) 기준.
# 자막을 영상이 아닌 캔버스에 burn 하므로, 자막 좌표(margin_v 등)도 캔버스 기준.
_ASS_PLAY_RES_X = CANVAS_W   # 1080
_ASS_PLAY_RES_Y = CANVAS_H   # 1920


def _srt_to_ass(
    srt_path: Path,
    ass_path: Path,
    *,
    font_name: str,
    alignment: int,
    font_size: int,
    primary: str,         # ASS 형식: &HBBGGRR&
    outline: str,
    outline_w: int,
    margin_v: int,
    bold: bool = True,
    play_res_x: int = _ASS_PLAY_RES_X,
    play_res_y: int = _ASS_PLAY_RES_Y,
) -> None:
    """SRT → ASS 변환.

    ASS 파일이 자체에 style 정보 포함 → ffmpeg subtitles 필터에서 force_style 불필요.
    이렇게 하면 filter_complex 의 ',' escape 문제 회피.

    SRT 안의 ASS 인라인 태그 ({\\an8} 같은 거) 는 그대로 보존됨.
    """
    text = srt_path.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", text.strip())

    bold_val = -1 if bold else 0  # ASS: -1=true, 0=false

    # ASS Style format (V4+):
    # Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,
    # BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing,
    # Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
    style_default = (
        f"Default,{font_name},{font_size},"
        f"{_ass_color(primary)},&H000000FF&,{_ass_color(outline)},&H00000000&,"
        f"{bold_val},0,0,0,100,100,0,0,1,{outline_w},0,"
        f"{alignment},10,10,{margin_v},1"
    )

    out_lines: list[str] = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {style_default}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    ts_re = re.compile(
        r"(\d+):(\d+):(\d+)[,\.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,\.](\d+)"
    )

    for block in blocks:
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            continue

        # 첫 줄은 보통 인덱스, 두 번째가 타임스탬프 — 단 일부 SRT 는 인덱스 없음
        ts_line = None
        text_start = 0
        if ts_re.search(lines[0]):
            ts_line = lines[0]
            text_start = 1
        elif len(lines) >= 2 and ts_re.search(lines[1]):
            ts_line = lines[1]
            text_start = 2
        else:
            continue

        m = ts_re.search(ts_line)
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()

        # ASS 시각: H:MM:SS.cc (2자리 centisecond)
        cc1 = ms1[:2].ljust(2, "0")
        cc2 = ms2[:2].ljust(2, "0")
        start_ass = f"{int(h1)}:{m1.zfill(2)}:{s1.zfill(2)}.{cc1}"
        end_ass = f"{int(h2)}:{m2.zfill(2)}:{s2.zfill(2)}.{cc2}"

        # 텍스트: 줄바꿈을 \N 으로
        text_lines = "\\N".join(lines[text_start:])
        # ★ 비-BMP 이모지 (🎶🐶🔥 등) 만 Noto Emoji 로 inline 전환 — Noto Sans CJK KR
        # 가 비-BMP pictographs 글리프 미보유 → tofu 방지. BMP 안 ♪♫★♥ 은 무관.
        text_lines = _inject_emoji_fontname(text_lines, main_font=font_name)

        out_lines.append(
            f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text_lines}"
        )

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _escape_ffmpeg_path(p: Path | str) -> str:
    """ffmpeg filter argument 안의 path escape: ':' → '\\:'."""
    return str(p).replace("\\", "\\\\").replace(":", "\\:")


def _composite_with_template(
    full_raw: Path,
    template_png: Path,
    video_zone: tuple[int, int, int, int],
    dialog_srt: Optional[Path],
    explain_srt: Optional[Path],
    plan: EditPlan,
    font_dir: Path,
    out_path: Path,
    *,
    encoding: dict,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
    is_cancelled: CancelCb | None = None,
) -> None:
    """v1.2 — 합성 → 자막 burn 순서.

    Step 1: 1:1 영상(full_raw) → 1080×1920 캔버스 padding + template overlay
    Step 2: 캔버스에 자막 burn (ASS PlayRes 1080×1920 기준)

    이전 v1.1 은 영상에 자막 박은 후 합성했지만, v1.2 는 자막을 캔버스 좌표에
    박아 자막 영역 (1400~1840) 같은 캔버스 외부 영역에도 자유롭게 위치 가능.
    """
    if is_cancelled and is_cancelled():
        raise EditCancelled()

    zx, zy, zw, zh = video_zone
    pad_top = zy
    pad_left = zx

    output_dir = out_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # ───────────────────────────────────────────────────────────────────
    # Step 1: 영상 → 1080×1920 캔버스 + template overlay (자막 없음)
    # ───────────────────────────────────────────────────────────────────
    # full_raw 는 이미 1080×1080 (sub-cut 단계에서 1:1 crop+scale 됨).
    # 안전을 위해 한 번 더 scale + setsar=1 (입력 변동 흡수).
    fc = (
        f"[0:v]scale={zw}:{zh}:force_original_aspect_ratio=decrease,"
        f"pad={zw}:{zh}:(ow-iw)/2:(oh-ih)/2:white,setsar=1[v_zoned];"
        f"[v_zoned]pad={canvas_w}:{canvas_h}:{pad_left}:{pad_top}:white[v_canvas];"
        f"[v_canvas][1:v]overlay=0:0,format=yuv420p[v_canvas_out]"
    )

    needs_subs = (
        (dialog_srt and dialog_srt.exists())
        or (explain_srt and explain_srt.exists())
    )
    canvas_out = output_dir / ("_temp_canvas.mp4" if needs_subs else out_path.name)

    cmd_composite = [
        _resolve_bin("ffmpeg"), "-y",
        "-i", str(full_raw),
        "-i", str(template_png),
        "-filter_complex", fc,
        "-map", "[v_canvas_out]",
        "-map", "0:a?",
        "-c:v", encoding.get("video_codec", "libx264"),
        "-preset", encoding.get("preset", "medium"),
        "-crf", str(encoding.get("crf", 18)),
        "-c:a", encoding.get("audio_codec", "aac"),
        "-b:a", encoding.get("audio_bitrate", "192k"),
        "-pix_fmt", encoding.get("pix_fmt", "yuv420p"),
        str(canvas_out),
        "-loglevel", "error",
    ]
    r1 = subprocess.run(cmd_composite, capture_output=True, text=True)
    if r1.returncode != 0:
        raise EditError(
            f"캔버스 합성 (Step 1) 실패:\n{r1.stderr.strip()[:800]}"
        )

    if not needs_subs:
        return

    # ───────────────────────────────────────────────────────────────────
    # Step 2: 캔버스 위에 자막 burn (ASS, PlayRes = 캔버스 1080×1920)
    # ───────────────────────────────────────────────────────────────────
    # v1.2 — schema 기본값 강제. plan 의 dialog_style/explain_style 무시.
    # (코워크가 plan-level override 가 필요하면 추후 enable.)
    ds = _default_dialog_style()
    es = _default_explain_style()

    dialog_ass: Optional[Path] = None
    explain_ass: Optional[Path] = None

    if dialog_srt and dialog_srt.exists():
        dialog_ass = output_dir / (dialog_srt.stem + ".ass")
        _srt_to_ass(
            dialog_srt, dialog_ass,
            font_name=ds.font_name,
            alignment=ds.alignment,
            font_size=ds.font_size,
            primary=ds.primary_colour,
            outline=ds.outline_colour,
            outline_w=ds.outline,
            margin_v=ds.margin_v,
            bold=bool(ds.bold),
        )

    if explain_srt and explain_srt.exists():
        explain_ass = output_dir / (explain_srt.stem + ".ass")
        _srt_to_ass(
            explain_srt, explain_ass,
            font_name=es.font_name,
            alignment=es.alignment,
            font_size=es.font_size,
            primary=es.primary_colour,
            outline=es.outline_colour,
            outline_w=es.outline,
            margin_v=es.margin_v,
            bold=bool(es.bold),
        )

    sub_filters: list[str] = []
    if dialog_ass:
        sub_filters.append(f"subtitles=f={dialog_ass.name}")
    if explain_ass:
        sub_filters.append(f"subtitles=f={explain_ass.name}")

    cmd_burn = [
        _resolve_bin("ffmpeg"), "-y",
        "-i", canvas_out.name,                  # cwd 기준
        "-vf", ",".join(sub_filters),
        "-c:v", encoding.get("video_codec", "libx264"),
        "-preset", encoding.get("preset", "medium"),
        "-crf", str(encoding.get("crf", 18)),
        "-c:a", "copy",                         # 오디오 재인코딩 X
        out_path.name,                           # cwd 기준
        "-loglevel", "error",
    ]
    r2 = subprocess.run(
        cmd_burn,
        capture_output=True, text=True,
        cwd=str(output_dir),                    # ★ cwd — libass path parser 안전
    )
    if r2.returncode != 0:
        raise EditError(
            f"자막 burn (Step 2) 실패:\n{r2.stderr.strip()[:800]}"
        )

    # 임시 파일 정리
    try:
        if canvas_out.exists() and canvas_out != out_path:
            canvas_out.unlink()
    except Exception:
        pass


# =============================================================================
# Renderer 추상화
# =============================================================================

class Renderer(ABC):
    """편집점 → mp4 렌더러 백엔드.

    v1.0: FfmpegRenderer (기본)
    v1.1+: CapCutDraftAdapter (CapCut draft JSON 으로 export, 사용자 미세조정 흐름)
    """

    @abstractmethod
    def render(
        self,
        plan: EditPlan,
        source_video: Path,
        output_dir: Path,
        *,
        channel: ChannelConfig,
        font_path: Path,
        on_progress: ProgressCb | None = None,
        is_cancelled: CancelCb | None = None,
    ) -> Path:
        """편집 실행. output_dir 안에 산출물 만들고 full.mp4 경로 반환."""


class FfmpegRenderer(Renderer):
    """ffmpeg + PIL 기본 렌더러. v1.0 의 메인 백엔드."""

    def render(
        self,
        plan: EditPlan,
        source_video: Path,
        output_dir: Path,
        *,
        channel: ChannelConfig,
        font_path: Path,
        apply_focus_box: bool = True,    # v1.6 — capcut export 모드면 False
        on_progress: ProgressCb | None = None,
        is_cancelled: CancelCb | None = None,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        cuts_dir = output_dir / "cuts"
        cuts_dir.mkdir(exist_ok=True)

        encoding = {
            "video_codec": plan.encoding.video_codec,
            "preset": plan.encoding.preset,
            "crf": plan.encoding.crf,
            "audio_codec": plan.encoding.audio_codec,
            "audio_bitrate": plan.encoding.audio_bitrate,
            "pix_fmt": plan.encoding.pix_fmt,
        }

        def emit(step: str, percent: float = 0.0,
                 message: str = "", elapsed: float = 0.0) -> None:
            if on_progress:
                on_progress(EditProgressEvent(
                    step=step, percent=percent, message=message, elapsed_s=elapsed,
                ))

        # 1. 폰트
        emit("font", 0, "한글 폰트 확인 중")
        ensure_font(font_path)

        # 2. 템플릿 PNG
        emit("template", 0, "템플릿 합성 시작")
        t0 = time.time()
        template_png, video_zone = render_template_png(
            plan, channel,
            output_dir / "template.png",
            font_path=font_path,
        )
        emit("template", 100, f"template.png 생성", time.time() - t0)

        # 3. sub-cut 추출
        sub_cuts: list[EditPlanSubCut] = list(plan.sub_cuts)
        if not sub_cuts:
            sub_cuts = [EditPlanSubCut(
                index=1,
                start=plan.shorts.start_s,
                end=plan.shorts.end_s,
                duration=plan.shorts.duration_s,
            )]

        # v1.4 — face_clusters 로드 (wide shot 자동 hide_caption 안전망용)
        face_clusters_for_render: dict | None = None
        try:
            import json as _json
            ad = Path(plan.source.analysis_dir).expanduser()
            fc_path = ad / "face_clusters.json"
            if fc_path.exists():
                face_clusters_for_render = _json.loads(
                    fc_path.read_text(encoding="utf-8")
                )
        except Exception:
            face_clusters_for_render = None

        cut_paths: list[Path] = []
        emit("subcut", 0, f"{len(sub_cuts)}개 sub-cut 추출 시작")
        t0 = time.time()
        for i, c in enumerate(sub_cuts, 1):
            cut_path = cuts_dir / f"cut{c.index:02d}.mp4"
            # v1.6 — apply_focus_box=False (capcut 모드) 면 focus_box 무시 + wide shot 보정도 skip
            if apply_focus_box:
                adjusted_focus_box = _adjust_focus_box_wide_shot(
                    c.focus_box, c.start, c.end, face_clusters_for_render,
                )
            else:
                adjusted_focus_box = None  # 영상 원본 비율 유지
            fb_label = ""
            if adjusted_focus_box:
                fb_label = (
                    f" zoom({adjusted_focus_box.x:.2f},{adjusted_focus_box.y:.2f},"
                    f"{adjusted_focus_box.w:.2f}×{adjusted_focus_box.h:.2f}"
                    f"|{adjusted_focus_box.reason or ''})"
                )
            elif not apply_focus_box:
                fb_label = " (capcut: 원본 비율)"
            _ffmpeg_extract_subcut(
                source_video, c.start, c.end, cut_path,
                encoding=encoding,
                focus_box=adjusted_focus_box,
                apply_focus_box=apply_focus_box,
                is_cancelled=is_cancelled,
            )
            cut_paths.append(cut_path)
            emit("subcut",
                 i / len(sub_cuts) * 100,
                 f"{i}/{len(sub_cuts)} cut{c.index:02d}.mp4 ({c.duration:.1f}초){fb_label}",
                 time.time() - t0)

        # 4. concat → full_raw.mp4
        full_raw = output_dir / "full_raw.mp4"
        emit("concat", 0, "sub-cut 합치기")
        t0 = time.time()
        if len(cut_paths) == 1:
            shutil.copy(cut_paths[0], full_raw)
        else:
            _ffmpeg_concat(cut_paths, full_raw)
        emit("concat", 100, "full_raw.mp4 완료", time.time() - t0)

        # 5. 자막 + 템플릿 합성 → full.mp4
        dialog_srt = output_dir / plan.subtitles.dialog_srt_file
        explain_srt = output_dir / plan.subtitles.explain_srt_file
        full_mp4 = output_dir / "full.mp4"

        emit("composite", 0, "자막+템플릿 합성")
        t0 = time.time()
        _composite_with_template(
            full_raw, template_png, video_zone,
            dialog_srt if dialog_srt.exists() else None,
            explain_srt if explain_srt.exists() else None,
            plan, font_path.parent, full_mp4,
            encoding=encoding, is_cancelled=is_cancelled,
        )
        emit("composite", 100, "full.mp4 완료", time.time() - t0)

        return full_mp4


class CapCutDraftAdapter(Renderer):
    """v1.6 — edit_plan.json → CapCut draft 생성 (VectCutAPI HTTP 호출).

    sun-guannan/VectCutAPI 의 capcut_server.py (port 9000) 가 별도 띄워져있어야 함.
    설치:
        cd ~/showdon
        git clone https://github.com/sun-guannan/VectCutAPI.git
        cd VectCutAPI && pip install -r requirements.txt
    실행:
        python capcut_server.py    # port 9000

    macOS CapCut 8.5 의 draft_info.json format 호환 (IS_CAPCUT_ENV 분기).
    save_draft 호출 시 dfd_<id>/ 폴더 생성됨 → 우리가 CapCut Drafts 폴더로 복사.
    사용자가 CapCut 열어서 자유 편집 후 CapCut UI Export 버튼으로 영상 출력.

    return: CapCut Drafts 폴더 안의 새 draft 경로
    """

    def __init__(
        self,
        capcut_drafts_path: str = "~/Movies/CapCut/User Data/Projects/com.lveditor.draft",
        server_url: str = "http://localhost:9000",
        server_cwd: str = "~/showdon/showdon-yejja/VectCutAPI",
        canvas_w: int = 1080,
        canvas_h: int = 1920,
        spawn_server: bool = True,
    ) -> None:
        super().__init__()
        self.capcut_drafts_path = capcut_drafts_path
        self.server_url = server_url.rstrip("/")
        self.server_cwd = server_cwd
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self.spawn_server = spawn_server   # False 면 사용자가 직접 띄운 서버만 사용
        # ★ v1.6.8 — 서버 lifecycle 관리 (우리가 spawn 한 거만 종료)
        self._spawned_proc: Optional[subprocess.Popen] = None
        self._spawned_log = None

    def _is_server_running(self) -> bool:
        """server_url 에 GET / 시도해서 connection 되면 True (200/404 무관)."""
        try:
            import requests
        except ImportError:
            return False
        try:
            requests.get(self.server_url + "/", timeout=2.0)
            return True
        except Exception:
            return False

    @staticmethod
    def _cleanup_old_dfd_dirs(
        server_cwd: Path,
        keep_days: float = 1.0,
    ) -> tuple[int, int]:
        """★ v1.9 — VectCutAPI 가 만드는 dfd_cat_* 폴더 안전망 정리.

        v1.6~v1.8 의 copytree 패턴 + 이전 실패한 export 잔재로 dfd_cat_* 가
        영구 누적되는 케이스 발견 (74GB 까지). v1.9 부터:
        - 각 export 끝 (move) 에 즉시 정리 (메인 흐름)
        - 추가 안전망: export 시작 시 N일 이상 된 dfd_cat_* 정리

        Returns:
            (cleaned_count, total_freed_bytes)
        """
        import time
        if not server_cwd.exists():
            return (0, 0)
        cutoff = time.time() - keep_days * 86400
        cleaned = 0
        freed = 0
        for d in server_cwd.glob("dfd_cat_*"):
            try:
                if not d.is_dir():
                    continue
                if d.stat().st_mtime >= cutoff:
                    continue   # 최근 것 — 보존 (혹시 다른 export 진행 중)
                # 디스크 사용량 측정 (du)
                size = 0
                try:
                    for p in d.rglob("*"):
                        if p.is_file():
                            size += p.stat().st_size
                except Exception:
                    pass
                shutil.rmtree(d, ignore_errors=True)
                cleaned += 1
                freed += size
            except Exception:
                pass
        return (cleaned, freed)

    def _ensure_server_started(
        self,
        *,
        on_progress: ProgressCb | None = None,
    ) -> None:
        """이미 켜져있으면 skip. 안 켜져있으면 백그라운드로 spawn 후 ready 까지 대기.

        ★ v1.6.8 — 사용자가 별도 터미널 띄울 필요 없게 자동화.
        - 이미 켜진 서버 (= 사용자 직접 실행한 것) 는 그대로 사용 + 우리가 안 끔
        - 우리가 spawn 한 거만 self._spawned_proc 보관 → render() 끝나면 종료
        - stdout/stderr 는 server_cwd/server.log 로 redirect (터미널 안 뜸)
        - start_new_session=True → 새 process group 으로 띄워 부모 .app 와 분리
        """
        if not self.spawn_server:
            return
        if self._is_server_running():
            if on_progress:
                on_progress(EditProgressEvent(
                    step="capcut_server", percent=1,
                    message="VectCutAPI server 이미 실행 중 (skip spawn)",
                ))
            return
        server_cwd = Path(self.server_cwd).expanduser()
        server_script = server_cwd / "capcut_server.py"
        if not server_script.exists():
            raise EditError(
                f"VectCutAPI capcut_server.py 없음: {server_script}\n"
                f"설치: cd ~/showdon && git clone "
                f"https://github.com/sun-guannan/VectCutAPI.git"
            )
        log_path = server_cwd / "server.log"
        try:
            log_f = log_path.open("ab")
        except Exception as e:
            raise EditError(
                f"server.log open 실패 ({log_path}): {e}"
            ) from e
        try:
            proc = subprocess.Popen(
                [sys.executable, str(server_script)],
                cwd=str(server_cwd),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            log_f.close()
            raise EditError(
                f"VectCutAPI server spawn 실패 (python={sys.executable}): {e}"
            ) from e
        self._spawned_proc = proc
        self._spawned_log = log_f
        if on_progress:
            on_progress(EditProgressEvent(
                step="capcut_server", percent=1,
                message=(
                    f"VectCutAPI server spawn (PID {proc.pid}, "
                    f"log: {log_path})"
                ),
            ))
        # ready 까지 polling (max 30초)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # 서버가 시작 직후 죽음 — log tail 로 원인 표시
                log_tail = ""
                try:
                    log_tail = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    )[-800:]
                except Exception:
                    pass
                self._spawned_proc = None
                try:
                    log_f.close()
                except Exception:
                    pass
                self._spawned_log = None
                raise EditError(
                    f"VectCutAPI server 시작 직후 죽음 "
                    f"(exit={proc.returncode}). log tail:\n{log_tail}"
                )
            if self._is_server_running():
                if on_progress:
                    on_progress(EditProgressEvent(
                        step="capcut_server", percent=2,
                        message="VectCutAPI server ready",
                    ))
                return
            time.sleep(0.5)
        # timeout
        self._stop_server_if_owned()
        raise EditError(
            f"VectCutAPI server 30초 안에 ready 안 됨. log: {log_path}"
        )

    def _stop_server_if_owned(self) -> None:
        """우리가 spawn 한 서버만 종료 (SIGTERM → 5초 후 SIGKILL)."""
        proc = self._spawned_proc
        log_f = self._spawned_log
        self._spawned_proc = None
        self._spawned_log = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    # process group 통째로 죽이기 (start_new_session=True 대응)
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        try:
                            proc.wait(timeout=2.0)
                        except Exception:
                            pass
            except Exception:
                pass
        if log_f is not None:
            try:
                log_f.close()
            except Exception:
                pass

    @staticmethod
    def _remap_srt_to_timeline(
        srt_text: str,
        sub_cuts: list,
        shorts_start_s: float,
        timeline_total: float,
    ) -> tuple[str, bool]:
        """source 시간 (sub_cut 갭 포함, 후보 구간 상대) SRT → timeline 시간 SRT.

        ★ v1.6.9 — 코워크 v1.6 룰 "후보 구간 시작 0 기준 상대 시각" 을
        source 시간 (sub_cut 갭 포함, 후보 끝 = end_s - start_s) 으로 해석하는
        케이스 안전망. video segment 는 갭 제거 후 timeline 0~timeline_total
        에 contiguous 박혀있는데 SRT 가 source 시간이면 mismatch (검은 화면 +
        자막 둥둥) 발생. SRT 의 max_ts 가 timeline_total 초과 시만 변환 적용.

        갭 안에 떨어지는 ts 는 가장 가까운 sub_cut 경계로 clamp.

        Returns: (remapped_srt_text, was_remapped)
        """
        import re as _re

        # 빠른 판정 — max ts 추출
        ts_pat = _re.compile(
            r'(\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})'
        )

        def _parse_ts(s: str) -> float:
            m = _re.match(
                r'(\d+):(\d{2}):(\d{2})[,.](\d{1,3})', s.strip()
            )
            if not m:
                return 0.0
            h, mi, se, ms = m.groups()
            return (
                int(h) * 3600 + int(mi) * 60 + int(se)
                + int(ms.ljust(3, '0')) / 1000.0
            )

        def _fmt_ts(t: float) -> str:
            if t < 0:
                t = 0.0
            h = int(t // 3600)
            mi = int((t % 3600) // 60)
            se = t % 60
            return f"{h:02d}:{mi:02d}:{se:06.3f}".replace('.', ',')

        # 모든 ts 추출해 max 찾기
        all_ts = []
        for m in ts_pat.finditer(srt_text):
            all_ts.append(_parse_ts(m.group(1)))
            all_ts.append(_parse_ts(m.group(2)))
        if not all_ts:
            return srt_text, False
        max_ts = max(all_ts)

        # timeline_total 안 들어오면 pass-through (코워크가 이미 timeline 시간)
        # 5% 마진 — float 누적 오차 흡수
        if max_ts <= timeline_total * 1.05:
            return srt_text, False

        # source → timeline mapping 함수
        cuts = []
        tl_off = 0.0
        for c in sub_cuts:
            src_s_rel = c.start - shorts_start_s
            src_e_rel = c.end - shorts_start_s
            dur = c.end - c.start
            cuts.append((src_s_rel, src_e_rel, tl_off))
            tl_off += dur

        def _map_t(src_t: float) -> float:
            for src_s, src_e, off in cuts:
                if src_s <= src_t <= src_e:
                    return off + (src_t - src_s)
            # 갭 안 → 가장 가까운 sub_cut 경계로 clamp
            best_t = 0.0
            best_dist = float('inf')
            for src_s, src_e, off in cuts:
                for src_b, tl_b in [
                    (src_s, off), (src_e, off + (src_e - src_s))
                ]:
                    d = abs(src_b - src_t)
                    if d < best_dist:
                        best_dist = d
                        best_t = tl_b
            return best_t

        # SRT 블록 단위 파싱 + 재작성
        blocks = _re.split(r'\n\s*\n', srt_text.strip())
        out_blocks = []
        new_idx = 1
        for block in blocks:
            lines = [ln for ln in block.split('\n') if ln.strip() != '']
            if len(lines) < 2:
                continue
            ts_line_idx = 1 if _re.match(r'^\d+$', lines[0].strip()) else 0
            if ts_line_idx >= len(lines):
                continue
            ts_match = ts_pat.search(lines[ts_line_idx])
            if not ts_match:
                continue
            src_s = _parse_ts(ts_match.group(1))
            src_e = _parse_ts(ts_match.group(2))
            body_lines = lines[ts_line_idx + 1:]
            if not body_lines:
                continue
            new_s = _map_t(src_s)
            new_e = _map_t(src_e)
            # 끝이 시작보다 앞 (또는 동일) 이면 0.5초 minimum
            if new_e <= new_s:
                new_e = min(new_s + 0.5, timeline_total)
            new_s = min(max(0.0, new_s), timeline_total)
            new_e = min(max(0.0, new_e), timeline_total)
            if new_e <= new_s:
                continue
            out_blocks.append(
                f"{new_idx}\n{_fmt_ts(new_s)} --> {_fmt_ts(new_e)}\n"
                + '\n'.join(body_lines)
            )
            new_idx += 1
        return '\n\n'.join(out_blocks) + '\n', True

    def _post(self, endpoint: str, payload: dict, timeout: float = 30.0) -> dict:
        """VectCutAPI HTTP POST 헬퍼."""
        try:
            import requests
        except ImportError as e:
            raise EditError("requests 미설치. pip install requests") from e
        url = f"{self.server_url}{endpoint}"
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            raise EditError(
                f"VectCutAPI server 연결 실패 ({url}). "
                f"별도 터미널에서 'cd ~/showdon/VectCutAPI && python capcut_server.py' "
                f"먼저 실행해주세요."
            ) from e
        if r.status_code != 200:
            raise EditError(
                f"VectCutAPI {endpoint} 실패 (HTTP {r.status_code}): "
                f"{r.text[:300]}"
            )
        try:
            data = r.json()
        except Exception as e:
            raise EditError(f"VectCutAPI {endpoint} 응답 JSON 아님: {r.text[:200]}") from e
        # VectCutAPI 응답 format: {"success": bool, "result": {...}, "error": "..."}
        if isinstance(data, dict) and data.get("success") is False:
            raise EditError(
                f"VectCutAPI {endpoint} 에러: {data.get('error', 'unknown')}"
            )
        return data

    def render(
        self,
        plan: EditPlan,
        source_video: Path,
        output_dir: Path,
        *,
        channel: ChannelConfig,
        font_path: Path,
        apply_focus_box: bool = False,
        on_progress: ProgressCb | None = None,
        is_cancelled: CancelCb | None = None,
    ) -> Path:
        """v1.6.8 wrapper — 서버 자동 spawn/terminate.

        실제 본체는 _render_impl. 이미 켜진 서버 (= 사용자 직접 실행) 는
        그대로 사용 + 우리가 안 끔. 우리가 spawn 한 거만 작업 끝나면 종료.
        """
        self._ensure_server_started(on_progress=on_progress)
        # ★ v1.9 — 이전 누적 dfd_cat_* 안전망 정리 (1일 이상 된 것)
        try:
            server_cwd = Path(self.server_cwd).expanduser()
            cleaned, freed = self._cleanup_old_dfd_dirs(server_cwd, keep_days=1.0)
            if cleaned and on_progress:
                on_progress(EditProgressEvent(
                    step="capcut_cleanup", percent=2,
                    message=(
                        f"이전 dfd_cat_* {cleaned}개 정리됨 "
                        f"({freed / 1024 / 1024 / 1024:.1f} GB 회수)"
                    ),
                ))
        except Exception:
            pass   # 정리 실패해도 메인 export 는 진행
        try:
            return self._render_impl(
                plan, source_video, output_dir,
                channel=channel, font_path=font_path,
                apply_focus_box=apply_focus_box,
                on_progress=on_progress, is_cancelled=is_cancelled,
            )
        finally:
            self._stop_server_if_owned()

    def _render_impl(
        self,
        plan: EditPlan,
        source_video: Path,
        output_dir: Path,
        *,
        channel: ChannelConfig,
        font_path: Path,
        apply_focus_box: bool = False,    # capcut export 는 무조건 원본 비율
        on_progress: ProgressCb | None = None,
        is_cancelled: CancelCb | None = None,
    ) -> Path:
        def emit(step: str, percent: float = 0.0,
                 message: str = "", elapsed: float = 0.0) -> None:
            if on_progress:
                on_progress(EditProgressEvent(
                    step=step, percent=percent, message=message, elapsed_s=elapsed,
                ))

        emit("capcut_init", 0, "CapCut Drafts 폴더 + VectCutAPI server 확인")
        drafts_path = Path(self.capcut_drafts_path).expanduser()
        if not drafts_path.exists():
            raise EditError(
                f"CapCut Drafts 폴더 없음: {drafts_path}\n"
                f"CapCut 한 번 실행해서 폴더 자동 생성하거나, "
                f"AppConfig.capcut_drafts_path 설정 확인."
            )

        # draft 이름 — outputs 폴더 이름 (예: 260506_거미가_좀비_되면_조정석_충격답변)
        draft_name = output_dir.name or plan.outputs.folder_name or "쇼돈예짜_draft"

        # 1. create_draft — 빈 draft + draft_id 받기
        emit("capcut_init", 10, f"draft '{draft_name}' 생성")
        resp = self._post("/create_draft", {
            "width": self.canvas_w,
            "height": self.canvas_h,
        })
        # VectCutAPI 응답: { "error": "", "output": { "draft_id": "dfd_cat_xxx", ... }, "success": true }
        output = resp.get("output") or resp.get("result") or {}
        draft_id = output.get("draft_id")
        if not draft_id:
            raise EditError(f"create_draft 응답에 draft_id 없음: {resp}")
        emit("capcut_init", 15, f"draft_id={draft_id}")

        # 2. add_video — sub_cut 마다 비디오 segment 추가
        sub_cuts: list[EditPlanSubCut] = list(plan.sub_cuts)
        if not sub_cuts:
            sub_cuts = [EditPlanSubCut(
                index=1, start=plan.shorts.start_s, end=plan.shorts.end_s,
                duration=plan.shorts.duration_s,
            )]

        timeline_pos = 0.0
        for i, c in enumerate(sub_cuts, 1):
            if is_cancelled and is_cancelled():
                raise EditCancelled()
            duration_s = c.end - c.start
            self._post("/add_video", {
                "draft_id": draft_id,
                # 로컬 파일 경로. URL 도 가능하다 함 (video_url) — 일단 video_url 로 시도
                "video_url": str(source_video),
                "start": c.start,                  # 원본 영상 안 시작
                "end": c.end,                      # 원본 영상 안 끝
                "target_start": timeline_pos,      # timeline 위치
                "volume": 1.0,
            })
            timeline_pos += duration_s
            emit("capcut_video", 15 + (i / len(sub_cuts)) * 35,
                 f"sub_cut {i}/{len(sub_cuts)} ({duration_s:.1f}초)")

        # 3. add_subtitle — dialog.srt + explain.srt
        # ★ v1.6.4 — VectCutAPI 의 add_subtitle 은 'srt' 키로 자막 **내용** 받음.
        # 기존에 'srt_path' 키로 path 보내서 silent fail (server 가 None 으로 받음).
        dialog_srt = output_dir / plan.subtitles.dialog_srt_file
        explain_srt = output_dir / plan.subtitles.explain_srt_file

        if dialog_srt.exists():
            try:
                # ★ v1.6.9 — 코워크가 source 시간 (sub_cut 갭 포함) 으로 박은 SRT 면
                # timeline 시간으로 자동 변환. timeline 시간이면 pass-through.
                d_text, d_remapped = self._remap_srt_to_timeline(
                    dialog_srt.read_text(encoding="utf-8"),
                    sub_cuts, plan.shorts.start_s, timeline_pos,
                )
                self._post("/add_subtitle", {
                    "draft_id": draft_id,
                    "srt": d_text,
                    # ★ track_name 다른 값 → 새 트랙 분리. 같으면 두 번째가 덮어씀.
                    "track_name": "dialog",
                    "font_size": 8.0,
                    "font_color": "#FFFFFF",        # 흰
                    "border_color": "#000000",      # 검은 외곽선
                    "border_width": 6,
                    "transform_y": -0.65,           # 화면 하단부
                })
                emit(
                    "capcut_subs", 60,
                    f"dialog.srt 추가 ({dialog_srt.stat().st_size}B"
                    + (", source→timeline remap" if d_remapped else "")
                    + ")",
                )
            except EditError as e:
                emit("capcut_subs", 60, f"dialog.srt 실패 (skip): {e}")
        else:
            emit("capcut_subs", 60, f"dialog.srt 없음: {dialog_srt}")

        if explain_srt.exists():
            try:
                e_text, e_remapped = self._remap_srt_to_timeline(
                    explain_srt.read_text(encoding="utf-8"),
                    sub_cuts, plan.shorts.start_s, timeline_pos,
                )
                self._post("/add_subtitle", {
                    "draft_id": draft_id,
                    "srt": e_text,
                    # ★ dialog 와 다른 track_name → 분리된 트랙
                    "track_name": "explain",
                    "font_size": 12.0,
                    "font_color": "#FFFF00",        # 노랑
                    "border_color": "#000000",      # 검은 외곽선
                    "border_width": 8,
                    "transform_y": 0.5,             # 화면 상단
                })
                emit(
                    "capcut_subs", 75,
                    f"explain.srt 추가 ({explain_srt.stat().st_size}B"
                    + (", source→timeline remap" if e_remapped else "")
                    + ")",
                )
            except EditError as e:
                emit("capcut_subs", 75, f"explain.srt 실패 (skip): {e}")
        else:
            emit("capcut_subs", 75, f"explain.srt 없음: {explain_srt}")

        # 3-2. ★ v1.6.5 — 메인 타이틀 + 출처 footer 자막 트랙
        # FfmpegRenderer 모드는 PIL template.png 에 박지만 CapCut 모드는
        # 영상 원본 비율을 유지 (template.png 안 씀) → 별도 자막 트랙으로 추가.
        # 누락되면 사용자가 CapCut 에서 매번 직접 텍스트 만들어야 함.
        title_text = plan.template.title_text or (
            plan.titles[0].text if plan.titles else ""
        )
        if title_text and timeline_pos > 0:
            # ★ v1.6.6 — 영상 시작부터 끝까지 표시 (footer 와 동일 시간 범위).
            # 이전 3초 제한 → 중간에 들어온 시청자는 제목 못 봄. 쇼츠는 retention
            # 모니터링상 항상 제목이 떠 있어야 함 (사용자 피드백 2026-05-07).
            t_h = int(timeline_pos // 3600)
            t_m = int((timeline_pos % 3600) // 60)
            t_s = timeline_pos % 60
            title_end_ts = (
                f"{t_h:02d}:{t_m:02d}:{t_s:06.3f}".replace(".", ",")
            )
            title_srt = (
                f"1\n00:00:00,000 --> {title_end_ts}\n{title_text}\n"
            )
            try:
                self._post("/add_subtitle", {
                    "draft_id": draft_id,
                    "srt": title_srt,
                    "track_name": "title",
                    "font_size": 18.0,             # 큰 글자 (explain 12 보다 큼)
                    # ★ v1.9.2 — 검정 → 흰글씨, 흰 외곽선 → 검정 외곽선 (색감 통일)
                    "font_color": "#FFFFFF",
                    "border_color": "#000000",
                    "border_width": 4,
                    "transform_y": 0.78,           # 화면 최상단 (헤더 영역)
                })
                emit("capcut_subs", 78,
                     f"title 자막 추가 ('{title_text[:20]}...', "
                     f"0~{timeline_pos:.1f}초 영상 전체)")
            except EditError as e:
                emit("capcut_subs", 78, f"title 자막 실패 (skip): {e}")
        else:
            emit("capcut_subs", 78, "title_text 없음 (skip)")

        footer_text = plan.footer.source_text or ""
        if footer_text and timeline_pos > 0:
            # 영상 전체 (start=0, end=timeline_pos).
            f_h = int(timeline_pos // 3600)
            f_m = int((timeline_pos % 3600) // 60)
            f_s = timeline_pos % 60
            footer_end_ts = (
                f"{f_h:02d}:{f_m:02d}:{f_s:06.3f}".replace(".", ",")
            )
            footer_srt = (
                f"1\n00:00:00,000 --> {footer_end_ts}\n{footer_text}\n"
            )
            try:
                self._post("/add_subtitle", {
                    "draft_id": draft_id,
                    "srt": footer_srt,
                    "track_name": "footer",
                    "font_size": 6.0,              # 작은 글자 (dialog 8 보다 작음)
                    # ★ v1.9.2 — 회색 → 밝은 회색 + 검정 외곽선 (색감 통일)
                    "font_color": "#C8C8C8",
                    "border_color": "#000000",
                    "border_width": 2,
                    "transform_y": -0.92,          # 화면 최하단
                })
                emit("capcut_subs", 82,
                     f"footer 자막 추가 ('{footer_text}', 0~{timeline_pos:.1f}초)")
            except EditError as e:
                emit("capcut_subs", 82, f"footer 자막 실패 (skip): {e}")
        else:
            emit("capcut_subs", 82, "footer_text 없음 (skip)")

        # 4. save_draft — server 의 dfd_<id> 폴더 생성 (background task!)
        emit("capcut_save", 85, "draft 저장 요청 (background)")
        self._post("/save_draft", {"draft_id": draft_id}, timeout=120.0)

        server_cwd = Path(self.server_cwd).expanduser()
        dfd_path = server_cwd / draft_id

        # ★ v1.6.2 — background task 완료 대기 (race condition fix)
        # save_draft 가 200 응답 즉시 반환하지만 실제 파일 복사는 background.
        # assets/ 폴더 + draft_info.json 둘 다 생길 때까지 polling.
        import time
        max_wait = 120.0
        poll = 0.5
        elapsed = 0.0
        emit("capcut_save", 88, "background task 완료 대기 중...")
        while elapsed < max_wait:
            if dfd_path.exists():
                draft_info_ok = (dfd_path / "draft_info.json").exists()
                assets_ok = (dfd_path / "assets").exists() or (dfd_path / "Resources").exists()
                if draft_info_ok and assets_ok:
                    # 안전 마진 — 마지막 파일 쓰기 끝났는지
                    time.sleep(0.5)
                    emit("capcut_save", 92,
                         f"background 완료 ({elapsed:.1f}초): {dfd_path.name}")
                    break
            time.sleep(poll)
            elapsed += poll

        if not dfd_path.exists() or not (dfd_path / "draft_info.json").exists():
            raise EditError(
                f"save_draft background task timeout ({max_wait}초). "
                f"server_cwd={server_cwd}, draft_id={draft_id}\n"
                f"server 로그 확인 필요 (failed: ... 같은 에러 있는지)"
            )

        # 5. CapCut Drafts 폴더로 이동 (★ v1.9 — copy → move, dfd_cat 자동 정리)
        # 같은 디스크면 rename (instant). 다른 디스크면 copy + delete (자동).
        # 이전 v1.6~v1.8 의 copytree 패턴은 dfd_path 원본을 남겨 1.1GB+ 누적
        # (74GB 까지 발견됨). move 로 즉시 정리.
        emit("capcut_save", 95, f"CapCut Drafts 폴더로 이동")
        target = drafts_path / draft_name
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(dfd_path), str(target))

        # 5-1. ★ v1.6.3 — draft_info.json video path placeholder fix
        # VectCutAPI 가 materials.videos[].path 를
        #   "##_material_placeholder_XXXX_##"  로 두는데 CapCut 이 resolve 못함.
        # → 검은 썸네일 + "자료를 다운로드할 수 없습니다" 자막.
        # 우리가 직접 path 를 assets/video/<material_name> 절대경로로 rewrite.
        # 정상 reference draft (사용자가 CapCut 에서 직접 만든 것) 의 형식과 동일.
        import json
        import uuid
        draft_info_path = target / "draft_info.json"
        if draft_info_path.exists():
            try:
                di = json.loads(draft_info_path.read_text(encoding="utf-8"))
                videos = di.get("materials", {}).get("videos", [])
                assets_video = target / "assets" / "video"
                fixed = 0
                for v in videos:
                    cur = v.get("path", "") or ""
                    # placeholder OR empty OR file 없는 stale path → 모두 fix 대상
                    needs_fix = (
                        cur.startswith("##_material_placeholder")
                        or not cur.strip()
                        or not Path(cur).exists()
                    )
                    if not needs_fix:
                        continue
                    mname = v.get("material_name", "") or ""
                    cand = assets_video / mname if mname else None
                    if cand and cand.exists() and cand.is_file():
                        v["path"] = str(cand)
                    elif source_video and Path(source_video).exists():
                        # fallback: 원본 source_video 절대경로
                        v["path"] = str(source_video)
                    else:
                        # 더 이상 fallback 없음 — skip
                        continue
                    if not v.get("local_material_id"):
                        v["local_material_id"] = str(uuid.uuid4())
                    fixed += 1
                if fixed:
                    payload_text = json.dumps(di, ensure_ascii=False)
                    draft_info_path.write_text(payload_text, encoding="utf-8")
                    # CapCut 이 .bak / template-2.tmp 도 참조하므로 일관 유지
                    for sib in ("draft_info.json.bak", "template-2.tmp"):
                        sib_path = target / sib
                        if sib_path.exists():
                            sib_path.write_text(payload_text, encoding="utf-8")
                    emit("capcut_save", 97,
                         f"video path placeholder fix: {fixed}개 → assets/video/")
            except Exception as e:
                emit("capcut_save", 97,
                     f"draft_info.json rewrite 실패 (CapCut 수동 link 필요): "
                     f"{type(e).__name__}: {e}")

        # 6. ★ v1.6.7 — draft_cover.jpg 생성 (CapCut 프로젝트 리스트 썸네일)
        # 안 만들면 CapCut 이 draft 처음 열 때까지 프로젝트 리스트에 빈 썸네일.
        # 정상 draft (사용자가 CapCut 에서 직접 만든 것) 폴더에는 1080x1920 JPEG
        # 으로 들어있음. 우리는 첫 sub_cut 시작 시점 frame 을 ffmpeg 로 추출해 만듦.
        emit("capcut_save", 98, "draft_cover.jpg 생성 (영상 첫 frame)")
        cover_path = target / "draft_cover.jpg"
        cover_t = sub_cuts[0].start if sub_cuts else plan.shorts.start_s
        try:
            cover_proc = subprocess.run(
                [
                    _resolve_bin("ffmpeg"), "-y",
                    "-ss", f"{cover_t:.3f}",
                    "-i", str(source_video),
                    "-vframes", "1",
                    # 1080x1920 캔버스에 letterbox (정상 draft 와 동일 사이즈).
                    # 16:9 영상은 위아래 검은 띠, 9:16 세로 영상은 그대로 fit.
                    "-vf",
                    "scale=1080:1920:force_original_aspect_ratio=decrease,"
                    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
                    "-q:v", "2",
                    str(cover_path),
                ],
                check=True,
                capture_output=True,
                timeout=30.0,
            )
            if cover_path.exists() and cover_path.stat().st_size > 0:
                emit("capcut_save", 99,
                     f"draft_cover.jpg 생성 "
                     f"({cover_path.stat().st_size // 1024}KB, t={cover_t:.1f}초)")
            else:
                emit("capcut_save", 99,
                     f"draft_cover.jpg 빈 파일 (ffmpeg stderr: "
                     f"{cover_proc.stderr.decode('utf-8', errors='replace')[:200]})")
        except subprocess.CalledProcessError as e:
            emit("capcut_save", 99,
                 f"draft_cover.jpg ffmpeg 실패 (skip): "
                 f"{e.stderr.decode('utf-8', errors='replace')[:200]}")
        except Exception as e:
            emit("capcut_save", 99,
                 f"draft_cover.jpg 실패 (skip): {type(e).__name__}: {e}")

        emit("capcut_done", 100, f"CapCut draft 생성 완료: {target}")

        # output_dir 에 안내 파일
        info_path = output_dir / "_CAPCUT_DRAFT_PATH.txt"
        info_path.write_text(
            f"CapCut draft 생성됨:\n"
            f"  {target}\n\n"
            f"CapCut 앱 열면 '{draft_name}' draft 목록에 보임.\n"
            f"열어서 편집 후 CapCut Export 버튼으로 영상 출력.\n",
            encoding="utf-8",
        )

        return target


# =============================================================================
# Orchestrator — produce_short
# =============================================================================

def _resolve_path(template: str, *, data_root: str) -> Path:
    """{data_root} 치환 + ~ 확장."""
    return Path(template.replace("{data_root}", data_root)).expanduser()


def produce_short(
    plan_dir: Path | str,
    output_dir: Path | str,
    *,
    config: AppConfig | None = None,
    renderer: Renderer | None = None,
    apply_focus_box: bool = True,    # v1.6 — capcut export 모드면 False
    on_progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> Path:
    """편집점 폴더 → 완성 폴더로 mp4 생성.

    Args:
        plan_dir: 편집점 폴더 ({data_root}/<채널>/편집점/<날짜_제목>/).
            안에 edit_plan.json + dialog.srt + explain.srt 필요.
        output_dir: 완성 폴더 ({data_root}/<채널>/완성/<날짜_제목>/).
            없으면 자동 생성.
        config: AppConfig (없으면 기본값).
        renderer: Renderer (없으면 FfmpegRenderer).
        apply_focus_box: True (자동편집) — focus_box 적용 + 1:1 캔버스. False (capcut export) — 영상 원본 비율.
        on_progress: 단계별 진행률 콜백.
        is_cancelled: 취소 체크 콜백.

    Returns:
        full.mp4 경로.

    Raises:
        EditError: 영상·SRT·폰트 등 실패
        EditCancelled: 사용자 취소
    """
    plan_dir = Path(plan_dir)
    output_dir = Path(output_dir)
    config = config or AppConfig()
    renderer = renderer or FfmpegRenderer()

    plan_path = plan_dir / "edit_plan.json"
    if not plan_path.exists():
        raise EditError(f"edit_plan.json 없음: {plan_path}")

    plan = load_dataclass(EditPlan, plan_path)

    source_video = Path(plan.source.video_path).expanduser()
    if not source_video.exists():
        raise EditError(f"소스 영상 없음: {source_video}")

    output_dir.mkdir(parents=True, exist_ok=True)

    # SRT 마커 처리 — v1.3: face_clusters 있으면 expand_follow_markers (\\pos 변환), 없으면 strip
    dialog_in = plan_dir / plan.subtitles.dialog_srt_file
    explain_in = plan_dir / plan.subtitles.explain_srt_file
    dialog_out = output_dir / plan.subtitles.dialog_srt_file
    explain_out = output_dir / plan.subtitles.explain_srt_file

    # face_clusters 로드 (있으면 follow 마커 활성화)
    face_clusters_dict: dict | None = None
    try:
        analysis_dir = Path(plan.source.analysis_dir).expanduser()
        fc_path = analysis_dir / "face_clusters.json"
        if fc_path.exists():
            import json as _json
            face_clusters_dict = _json.loads(fc_path.read_text(encoding="utf-8"))
    except Exception:
        face_clusters_dict = None

    sub_cut_offset_s = float(plan.shorts.start_s)

    if dialog_in.exists():
        if face_clusters_dict:
            expand_follow_markers(
                dialog_in, dialog_out, face_clusters_dict,
                sub_cut_offset_s=sub_cut_offset_s,
            )
        else:
            strip_follow_markers(dialog_in, dialog_out)
    if explain_in.exists():
        if face_clusters_dict:
            expand_follow_markers(
                explain_in, explain_out, face_clusters_dict,
                sub_cut_offset_s=sub_cut_offset_s,
            )
        else:
            strip_follow_markers(explain_in, explain_out)

    # 폰트 경로 정규화
    font_path = _resolve_path(config.font_path, data_root=config.data_root)

    try:
        full_mp4 = renderer.render(
            plan, source_video, output_dir,
            channel=config.channel,
            font_path=font_path,
            apply_focus_box=apply_focus_box,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

        # produced meta.json
        produced = FolderMeta(
            kind=FolderMetaKind.PRODUCED_META,
            title=plan.candidate.key_phrase or plan.template.title_text.replace("\n", " "),
            date=plan.candidate.date_str,
            source_video_basename=source_video.stem,
            candidate_id=plan.candidate.id,
            duration_s=plan.shorts.duration_s,
            tone=plan.candidate.tone,
            score=plan.candidate.score,
        )
        save_dataclass(produced, output_dir / "meta.json")

        # ★ v0.1.2 — 디버그 모드: 자동편집 결과물 한 곳에 모아 비교 편하게.
        # 끄려면 config.debug_auto_output_dir = "" 또는 None 으로 (디버깅 탭에서 변경).
        # 원본 (output_dir/full.mp4) 은 그대로 두고 복사만.
        # CapCut 모드는 결과물이 .mp4 가 아니라 draft 폴더라서 자동 skip.
        # ★ v1.9.3 — 호출 시점에 _resolve_debug_auto_output_dir() 재평가
        #   (GUI 변경 즉시 반영 — config.json 매번 새로 읽음).
        debug_path = _resolve_debug_auto_output_dir()
        if (
            debug_path
            and full_mp4
            and full_mp4.suffix.lower() == ".mp4"
            and full_mp4.exists()
        ):
            try:
                debug_dir = Path(debug_path).expanduser()
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_dst = debug_dir / f"{output_dir.name}.mp4"
                shutil.copy2(full_mp4, debug_dst)
                if on_progress:
                    on_progress(EditProgressEvent(
                        step="debug_copy", percent=100,
                        message=(
                            f"디버그 복사 → {debug_dst} "
                            f"({debug_dst.stat().st_size // (1024*1024)}MB)"
                        ),
                    ))
            except Exception as e:
                if on_progress:
                    on_progress(EditProgressEvent(
                        step="debug_copy", percent=100,
                        message=f"디버그 복사 실패 (skip): {e}",
                    ))

        write_marker(output_dir, MARKER_DONE)
        return full_mp4

    except EditCancelled:
        # 취소는 마커 안 남김 (재시도 깨끗)
        raise
    except Exception as e:
        write_marker(output_dir, MARKER_FAILED, {"error": str(e)})
        raise


# =============================================================================
# 스모크 테스트 (heavy deps 없이 검증 가능한 부분만)
# =============================================================================

if __name__ == "__main__":
    import sys
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    print("=== edit.py 스모크 테스트 ===")

    # 1. strip_follow_markers
    print("\n[1] strip_follow_markers")
    with tempfile.TemporaryDirectory() as td:
        srt_in = Path(td) / "in.srt"
        srt_out = Path(td) / "out.srt"
        srt_in.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n(타블로 진심) [follow:2]\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n(투컷 당황) [follow:1 offset:y=-40]\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n(웃음)\n\n",
            encoding="utf-8",
        )
        n = strip_follow_markers(srt_in, srt_out)
        cleaned = srt_out.read_text(encoding="utf-8")
        assert n == 2, f"got {n}"
        assert "[follow:" not in cleaned
        assert "(타블로 진심)" in cleaned
        assert "(웃음)" in cleaned
        print(f"  [OK] {n}개 마커 제거, 본문 보존")

    # 2. _srt_to_ass 변환 + 새 디폴트 스타일 검증
    print("\n[2] _srt_to_ass + v1.2 default style")
    with tempfile.TemporaryDirectory() as td:
        srt_in = Path(td) / "in.srt"
        ass_out = Path(td) / "out.ass"
        srt_in.write_text(
            "1\n00:00:00,500 --> 00:00:02,000\n안녕하세요\n\n"
            "2\n00:00:03,000 --> 00:00:05,000\n두 번째 라인\n",
            encoding="utf-8",
        )
        ds = _default_dialog_style()
        _srt_to_ass(
            srt_in, ass_out,
            font_name=ds.font_name, alignment=ds.alignment,
            font_size=ds.font_size, primary=ds.primary_colour,
            outline=ds.outline_colour, outline_w=ds.outline,
            margin_v=ds.margin_v, bold=bool(ds.bold),
        )
        ass_text = ass_out.read_text(encoding="utf-8")
        assert "[V4+ Styles]" in ass_text
        assert "PlayResX: 1080" in ass_text
        assert "PlayResY: 1920" in ass_text
        assert "&H000000&" in ass_text       # 검정 primary
        assert "Dialogue: 0,0:00:00.50,0:00:02.00,Default" in ass_text
        assert "안녕하세요" in ass_text
        print("  [OK] dialog 디폴트 — 검정 텍스트, 캔버스 PlayRes 1080×1920")

        es = _default_explain_style()
        assert es.font_size == 90, f"explain font_size {es.font_size}"
        assert es.primary_colour == "&H00FFFF&"   # 노란
        assert es.alignment == 8
        print("  [OK] explain 디폴트 — 90pt 노란색 alignment=8")

    # 3. _resolve_path
    print("\n[3] _resolve_path")
    p = _resolve_path("{data_root}/_fonts/Noto.otf", data_root="/Users/joel/showdon/yejjas")
    assert str(p) == "/Users/joel/showdon/yejjas/_fonts/Noto.otf"
    p2 = _resolve_path("~/showdon/yejjas/_fonts/Noto.otf", data_root="/x")
    assert "/showdon/yejjas/_fonts/Noto.otf" in str(p2)
    print("  [OK] {data_root} 치환 + ~ 확장")

    # 4. PIL 템플릿 — PIL 깔려있으면 검증
    print("\n[4] render_template_png (PIL 있을 때만)")
    try:
        from PIL import Image  # noqa
        from .schema import (  # noqa
            ChannelConfig as _CC, EditPlan as _EP, EditPlanFooter, EditPlanTemplate,
        )
        plan = _EP()
        plan.template.title_text = "n년째 고용 중인\n투컷 친구 대행 알바"
        plan.footer.source_text = "출처 - 유병재"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "template.png"
            channel = _CC(handle="mumakeshigh", name_kr="뮤맥하")
            png_path, zone = render_template_png(plan, channel, out)
            assert png_path.exists()
            assert png_path.stat().st_size > 0
            x, y, w, h = zone
            assert (x, y, w, h) == (VIDEO_ZONE_X, VIDEO_ZONE_Y,
                                    VIDEO_ZONE_W, VIDEO_ZONE_H), \
                f"video_zone 변경 (예상: {(VIDEO_ZONE_X, VIDEO_ZONE_Y, VIDEO_ZONE_W, VIDEO_ZONE_H)}, 실제: {zone})"
            from PIL import Image as PILImage
            img = PILImage.open(png_path)
            assert img.size == (1080, 1920)
            assert img.mode == "RGBA"
            print(f"  [OK] template.png ({img.size}, mode={img.mode}, "
                  f"{png_path.stat().st_size:,} bytes), zone={zone}")
    except ImportError:
        print("  [SKIP] PIL 미설치")

    # 5. Renderer abstract base
    print("\n[5] Renderer ABC")
    try:
        Renderer()  # type: ignore
        assert False, "abstract 클래스가 인스턴스화돼서는 안 됨"
    except TypeError:
        print("  [OK] Renderer 는 abstract")

    # CapCutDraftAdapter v1.6 — pyCapCut 미설치면 EditError, 설치돼있으면 정상 인스턴스화
    adapter = CapCutDraftAdapter()
    assert adapter.canvas_w == 1080 and adapter.canvas_h == 1920
    assert adapter.capcut_drafts_path.endswith("com.lveditor.draft")
    print("  [OK] CapCutDraftAdapter v1.6 인스턴스화 (실제 render 는 pycapcut 설치 + 영상 필요)")

    print("\n=== 모든 스모크 테스트 통과 ===")
    print("\n실제 편집 테스트는 venv + ffmpeg + Pillow 환경에서:")
    print("  python -c 'from backend.edit import produce_short; "
          "produce_short(\"plan_dir\", \"out_dir\", on_progress=print)'")
