# -*- coding: utf-8 -*-
"""
backend/config.py — AppConfig 디스크 영속화.

저장 위치: ~/Library/Application Support/showdon-yejja/config.json (macOS).
없으면 기본값으로 생성. 손상돼있으면 .json.bak 으로 백업하고 새로 생성.

GUI 의 환경설정에서 일부 필드 변경 가능. config.json 을 직접 편집해도 OK.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

from .schema import AppConfig, load_dataclass, save_dataclass


# ★ v1.9.3 — VERSION 자동 동기화. SYSTEM_PROMPT_v*.md glob 으로 최신 버전 검출.
# 새 SYSTEM_PROMPT_v1.x.x.md 만들면 다음 GUI 실행 시 타이틀에 자동 반영.
# .archive/ 안 옛버전은 제외 (top-level 만 검색).
def _detect_latest_prompt_version() -> str:
    """~/showdon/showdon-yejja/SYSTEM_PROMPT_v*.md 검색 후 최신 버전 리턴.

    Fallback: glob 결과 X 면 "0.0.0" (이상 신호 — repo root 못 찾았거나 파일 없음).
    """
    repo_root = Path(__file__).resolve().parent.parent
    files = glob.glob(str(repo_root / "SYSTEM_PROMPT_v*.md"))

    versions = []
    for f in files:
        m = re.search(r"SYSTEM_PROMPT_v(\d+)\.(\d+)\.(\d+)\.md$", f)
        if m:
            versions.append(tuple(int(x) for x in m.groups()))

    if not versions:
        return "0.0.0"

    latest = max(versions)
    return f"{latest[0]}.{latest[1]}.{latest[2]}"


VERSION = _detect_latest_prompt_version()
APP_BUNDLE_ID = "com.showdon.yejja"

# ★ v1.9.3+ — detect_p_core_count() 제거됨 (초고성능 모드 폐지).
# mlx-whisper + Apple Vision 둘 다 GPU only 라 CPU thread 조절 영향 거의 없음.


def get_config_dir() -> Path:
    """macOS: ~/Library/Application Support/showdon-yejja/"""
    return (
        Path.home() / "Library" / "Application Support" / "showdon-yejja"
    ).expanduser()


def get_config_path() -> Path:
    return get_config_dir() / "config.json"


def load_config() -> AppConfig:
    """디스크에서 로드. 없거나 손상되면 기본값 + 디스크 저장."""
    p = get_config_path()
    if p.exists():
        try:
            return load_dataclass(AppConfig, p)
        except Exception:
            # 손상된 config 는 백업하고 새로 생성
            backup = p.with_suffix(".json.bak")
            try:
                p.rename(backup)
            except Exception:
                pass

    cfg = AppConfig()
    save_config(cfg)
    return cfg


def save_config(cfg: AppConfig) -> None:
    p = get_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    save_dataclass(cfg, p)


def resolve_data_root(cfg: AppConfig) -> Path:
    """data_root 의 ~ 확장 + 절대경로화."""
    return Path(cfg.data_root).expanduser().resolve()


# =============================================================================
# 폴더 구조 (v1.1 — 영상별 평탄화):
#   {data_root}/<영상명>/원본/video.mp4 + source_meta.json
#   {data_root}/<영상명>/분석/meta.json + stt.json + frames/ + ...
#   {data_root}/<영상명>/편집점/<날짜_제목>/edit_plan.json + dialog.srt + ...
#   {data_root}/<영상명>/완성/<날짜_제목>/full.mp4 + ...
# =============================================================================

def all_videos_root(cfg: AppConfig) -> Path:
    """모든 영상 폴더의 부모 = data_root."""
    return resolve_data_root(cfg)


def video_root(cfg: AppConfig, video_basename: str) -> Path:
    """영상 한 편의 모든 산출물이 모이는 부모 폴더.

    구조: {data_root}/<영상명>/{원본,분석,편집점,완성}/
    """
    return all_videos_root(cfg) / video_basename


def originals_dir_for(cfg: AppConfig, video_basename: str) -> Path:
    """{data_root}/<영상명>/원본/"""
    return video_root(cfg, video_basename) / "원본"


def analysis_dir_for(cfg: AppConfig, video_basename: str) -> Path:
    """{data_root}/<영상명>/분석/"""
    return video_root(cfg, video_basename) / "분석"


def edit_plans_dir_for(cfg: AppConfig, video_basename: str) -> Path:
    """{data_root}/<영상명>/편집점/"""
    return video_root(cfg, video_basename) / "편집점"


def completed_dir_for(cfg: AppConfig, video_basename: str) -> Path:
    """{data_root}/<영상명>/완성/"""
    return video_root(cfg, video_basename) / "완성"


# ==== 호환 헬퍼 (UI file dialog 초기 위치 등) ====
# 이전 API 시그니처 유지. 모두 data_root 를 반환 — 실제 영상별 위치는 *_for() 사용.

def originals_dir(cfg: AppConfig) -> Path:
    """[file dialog 용] data_root. 실제 다운로드는 originals_dir_for(cfg, basename)."""
    return all_videos_root(cfg)


def analysis_dir(cfg: AppConfig) -> Path:
    """[file dialog 용] data_root. 실제 분석 출력은 analysis_dir_for(cfg, basename)."""
    return all_videos_root(cfg)


def edit_plans_dir(cfg: AppConfig) -> Path:
    """[file dialog 용] data_root. 실제 편집점은 edit_plans_dir_for(cfg, basename)."""
    return all_videos_root(cfg)


def completed_dir(cfg: AppConfig) -> Path:
    """[file dialog 용] data_root. 실제 완성본은 completed_dir_for(cfg, basename)."""
    return all_videos_root(cfg)
