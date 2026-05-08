# -*- coding: utf-8 -*-
"""
파일명 규칙: yymmdd-platform-title.mp4

예: 260428-youtube-우아한_발레하는_고양이.mp4

- yymmdd: 영상 업로드 날짜 (yt-dlp upload_date) 우선, 없으면 다운로드 시각
- platform: youtube / tiktok / instagram / threads / x / unknown
- title: 영상 제목 — 특수문자 제거, 공백은 _ 로 치환, 길이 100자 제한

(showdon-downloader/backend/filename.py 에서 그대로 가져옴)
"""

import re
from datetime import datetime
from typing import Optional

# 파일명에서 금지된 문자 (macOS / Windows 양쪽 안전 집합)
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 연속 공백/마침표 정리
_WHITESPACE = re.compile(r"\s+")
_TITLE_MAX_LEN = 100


def sanitize_title(title: Optional[str]) -> str:
    """파일명에 안전한 형태로 제목을 정제. 공백은 _ 로 치환."""
    if not title:
        return "untitled"
    cleaned = _FORBIDDEN.sub("", title)
    # 모든 공백류(스페이스/탭/줄바꿈) → _ 로 치환 + 연속된 _ 는 하나로
    cleaned = _WHITESPACE.sub("_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    # 시작/끝의 마침표/언더스코어 정리 (hidden file 방지 + 깔끔)
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "untitled"
    if len(cleaned) > _TITLE_MAX_LEN:
        cleaned = cleaned[:_TITLE_MAX_LEN].rstrip("._")
    return cleaned


def format_yymmdd(upload_date: Optional[str]) -> str:
    """
    yt-dlp 의 upload_date (YYYYMMDD 문자열) → yymmdd.
    값이 없거나 잘못된 형식이면 오늘 날짜로 fallback.
    """
    if upload_date and len(upload_date) == 8 and upload_date.isdigit():
        # YYYYMMDD → YYMMDD
        return upload_date[2:]
    return datetime.now().strftime("%y%m%d")


def build_filename(*, upload_date: Optional[str], platform: str,
                   title: Optional[str], ext: str = "mp4") -> str:
    """
    yymmdd-platform-title.ext 형식의 파일명 (확장자 포함) 생성.
    파일 시스템에 안전한 이름.
    """
    yymmdd = format_yymmdd(upload_date)
    safe_title = sanitize_title(title)
    safe_platform = re.sub(r"[^a-z0-9]", "", (platform or "unknown").lower()) or "unknown"
    ext = (ext or "mp4").lstrip(".")
    return f"{yymmdd}-{safe_platform}-{safe_title}.{ext}"
