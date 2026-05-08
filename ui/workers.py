# -*- coding: utf-8 -*-
"""
ui/workers.py — QThread 기반 backend worker.

GUI 가 무거운 작업(다운로드·분석·편집)을 백그라운드에서 돌리고
시그널·슬롯으로 진행률 / 영상 완료 / 실패 알림.

각 worker 의 .cancel() 메서드는 backend 함수의 is_cancelled 콜백을 통해
즉시 또는 다음 영상 직전에 중단. (사용자가 GUI 에서 "취소" 누를 때 호출)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from backend.schema import AppConfig


# =============================================================================
# DownloadWorker — yt-dlp 다운로드
# =============================================================================

class DownloadWorker(QThread):
    """다운로드 큐 백그라운드 실행.

    Signals:
        progress(DownloadProgress)  — 영상별 진행 이벤트
        video_done(Path)            — 영상 1편 완료 (분석 큐에 자동 추가용)
        finished_all(list[Path])    — 큐 전체 종료 (성공한 폴더 리스트)
        failed(str)                 — 큐 자체 실패 (URL 파싱 등)
    """

    progress = Signal(object)
    video_done = Signal(object)
    finished_all = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        urls: list[str],
        originals_dir: Path,
        *,
        overwrite: bool = False,
    ) -> None:
        super().__init__()
        self.urls = list(urls)
        self.originals_dir = Path(originals_dir)
        self.overwrite = overwrite
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D401
        from backend.download import download_videos

        try:
            done = download_videos(
                self.urls,
                self.originals_dir,
                overwrite=self.overwrite,
                on_progress=lambda p: self.progress.emit(p),
                on_video_done=lambda folder: self.video_done.emit(folder),
                is_cancelled=lambda: self._cancel,
            )
            self.finished_all.emit(done)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# =============================================================================
# AnalyzeWorker — 1단계 분석 큐
# =============================================================================

class AnalyzeWorker(QThread):
    """분석 큐 백그라운드 실행. 한 영상 실패해도 다음 영상 계속.

    Signals:
        progress(AnalysisProgressEvent)  — 단계별 진행
        video_started(Path)              — 영상 분석 시작
        video_done(Path, AnalysisMeta)   — 영상 분석 완료
        video_failed(Path, str)          — 영상 분석 실패 (메시지)
        finished_all()                   — 큐 전체 종료
    """

    progress = Signal(object)
    video_started = Signal(object)         # video_file: Path
    video_done = Signal(object, object)    # (video_file, AnalysisMeta)
    video_failed = Signal(object, str)     # (video_file, error)
    finished_all = Signal()

    def __init__(
        self,
        items: list[dict],
        *,
        config: AppConfig,
    ) -> None:
        """
        Args:
            items: list of dict — 각 dict 는 다음 키:
                - video_file: Path  실제 mp4 파일
                - video_dir:  Path  영상 부모 폴더 (= 분석/, 편집점/, 완성/ 가 생성될 위치)
            config: AppConfig
        """
        super().__init__()
        self.items = list(items)
        self.config = config
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from backend.analyze import analyze_video, AnalysisCancelled

        for item in self.items:
            if self._cancel:
                break

            video_file: Path = item["video_file"]
            video_dir: Path = item["video_dir"]

            self.video_started.emit(video_file)

            output_dir = video_dir / "분석"
            source_meta_path = video_dir / "원본" / "source_meta.json"
            if not source_meta_path.exists():
                source_meta_path = None  # type: ignore[assignment]

            try:
                meta = analyze_video(
                    video_file, output_dir,
                    config=self.config,
                    on_progress=lambda e: self.progress.emit(e),
                    is_cancelled=lambda: self._cancel,
                    source_meta_path=source_meta_path,
                )
                self.video_done.emit(video_file, meta)
            except AnalysisCancelled:
                break
            except Exception as e:
                self.video_failed.emit(
                    video_file, f"{type(e).__name__}: {e}"
                )

        self.finished_all.emit()


# =============================================================================
# EditWorker — 4단계 편집
# =============================================================================

class EditWorker(QThread):
    """편집 1편 백그라운드 실행. 다중 편집점은 caller 가 순차 worker 인스턴스화.

    Signals:
        progress(EditProgressEvent)
        completed(Path)               — full.mp4 경로
        failed(str)
    """

    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        plan_dir: Path,
        output_dir: Path,
        *,
        config: AppConfig,
        export_mode: str = "auto",   # v1.6 — "auto" (자동편집) | "capcut" (CapCut draft)
    ) -> None:
        super().__init__()
        self.plan_dir = Path(plan_dir)
        self.output_dir = Path(output_dir)
        self.config = config
        self.export_mode = export_mode
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from backend.edit import (
            produce_short, EditCancelled,
            FfmpegRenderer, CapCutDraftAdapter,
        )

        try:
            # v1.6 — export_mode 따라 renderer 선택
            if self.export_mode == "capcut":
                renderer = CapCutDraftAdapter(
                    capcut_drafts_path=self.config.capcut_drafts_path,
                    server_url=getattr(
                        self.config, "capcut_server_url", "http://localhost:9000",
                    ),
                    server_cwd=getattr(
                        self.config, "capcut_server_cwd",
                        "~/showdon/showdon-yejja/VectCutAPI",
                    ),
                )
                apply_focus_box = False  # 캡컷은 영상 원본 비율
            else:
                renderer = FfmpegRenderer()
                apply_focus_box = True

            output = produce_short(
                self.plan_dir,
                self.output_dir,
                config=self.config,
                renderer=renderer,
                apply_focus_box=apply_focus_box,
                on_progress=lambda e: self.progress.emit(e),
                is_cancelled=lambda: self._cancel,
            )
            self.completed.emit(output)
        except EditCancelled:
            pass
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
