# -*- coding: utf-8 -*-
"""
ui/download_tab.py — 다운로드 탭.

URL (단일 / 멀티 / 플레이리스트) → yt-dlp → 영상 폴더 + source_meta.json.
완료된 영상은 video_downloaded Signal 로 분석 탭에 자동 전달 가능.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CheckBox, FluentIcon, MessageBox,
    PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton,
    SubtitleLabel, TitleLabel,
)

from backend.config import AppConfig, all_videos_root
from backend.download import DownloadProgress
from .workers import DownloadWorker


class DownloadTab(QWidget):
    """다운로드 탭.

    Signals:
        video_downloaded(Path) — 영상 폴더 완료 (분석 큐 자동 추가용)
    """

    video_downloaded = Signal(object)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setObjectName("downloadTab")
        self.config = config
        self.worker: DownloadWorker | None = None

        self._setup_ui()

    # ---------------------------------------------------------------- UI ----

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # 헤더
        title = TitleLabel("URL 다운로드")
        subtitle = BodyLabel(
            "YouTube · TikTok · Instagram · 플레이리스트 URL 을 한 줄에 한 개씩.\n"
            "각 영상은 yymmdd-platform-title.mp4 로 저장되고 source_meta.json 도 자동 생성돼요."
        )
        subtitle.setStyleSheet("color: #666;")
        root.addWidget(title)
        root.addWidget(subtitle)

        # URL 입력
        self.url_input = PlainTextEdit()
        self.url_input.setPlaceholderText(
            "https://www.youtube.com/watch?v=...\n"
            "https://www.youtube.com/playlist?list=...\n"
            "https://www.tiktok.com/@user/video/..."
        )
        self.url_input.setFixedHeight(150)
        root.addWidget(self.url_input)

        # 옵션
        opts_row = QHBoxLayout()
        self.auto_queue = CheckBox("다운로드 후 자동으로 분석까지 진행")
        self.auto_queue.setChecked(True)
        self.overwrite = CheckBox("이미 받은 영상이 있으면 덮어쓰기")
        self.overwrite.setChecked(False)
        opts_row.addWidget(self.auto_queue)
        opts_row.addSpacing(24)
        opts_row.addWidget(self.overwrite)
        opts_row.addStretch(1)
        root.addLayout(opts_row)

        # 버튼
        btn_row = QHBoxLayout()
        self.start_btn = PrimaryPushButton(FluentIcon.DOWNLOAD, "다운로드 시작")
        self.start_btn.clicked.connect(self._on_start)
        self.cancel_btn = PushButton(FluentIcon.CLOSE, "취소")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        # 진행 상태
        self.progress_label = BodyLabel("대기 중")
        self.progress_label.setStyleSheet("font-weight: 500;")
        self.progress_bar = ProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label = BodyLabel("")
        self.status_label.setStyleSheet("color: #888;")
        root.addWidget(self.progress_label)
        root.addWidget(self.progress_bar)
        root.addWidget(self.status_label)

        # 결과 테이블
        root.addWidget(SubtitleLabel("최근 다운로드"))
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(["시각", "제목", "상태", "위치"])
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.setAlternatingRowColors(True)
        root.addWidget(self.result_table, stretch=1)

    # --------------------------------------------------------- 핸들러 ---

    def _on_start(self) -> None:
        urls = [
            line.strip()
            for line in self.url_input.toPlainText().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not urls:
            MessageBox("URL 없음", "URL 을 한 줄에 한 개씩 입력해주세요.", self).exec()
            return

        target_dir = all_videos_root(self.config)
        target_dir.mkdir(parents=True, exist_ok=True)

        self.worker = DownloadWorker(
            urls, target_dir, overwrite=self.overwrite.isChecked()
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.video_done.connect(self._on_video_done)
        self.worker.finished_all.connect(self._on_finished_all)
        self.worker.failed.connect(self._on_failed)

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.url_input.setEnabled(False)
        self.progress_label.setText(f"다운로드 시작 — {len(urls)}개 URL")
        self.status_label.setText("")
        self.progress_bar.setValue(0)

        self.worker.start()

    def _on_cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("취소 요청 — 현재 영상 종료 후 멈춥니다")

    def _on_progress(self, p: DownloadProgress) -> None:
        if p.status == "fetching_meta":
            self.progress_label.setText(f"메타 추출 중: {p.url[:80]}")
            self.progress_bar.setValue(0)
            self.status_label.setText(p.message)
        elif p.status == "downloading":
            title = (p.title or p.url)[:60]
            self.progress_label.setText(f"다운로드 중: {title}")
            # ★ v1.9.5 — 같은 정수면 setValue skip (macOS 26.x + Qt 6.11 paint pipeline 충돌 회피)
            new_val = int(p.percent)
            if new_val != self.progress_bar.value():
                self.progress_bar.setValue(new_val)
            speed_eta = []
            if p.speed_str:
                speed_eta.append(p.speed_str)
            if p.eta_str:
                speed_eta.append(f"ETA {p.eta_str}")
            self.status_label.setText("  ·  ".join(speed_eta))
        elif p.status == "postprocessing":
            self.progress_bar.setValue(99)
            self.status_label.setText("머지 중 (영상 + 오디오)")
        elif p.status == "skipped":
            self._add_result(
                p.title or p.url, "스킵 (이미 존재)", str(p.folder or "")
            )
        elif p.status == "failed":
            self._add_result(p.title or p.url, f"실패: {p.error}", "")
        elif p.status == "cancelled":
            self._add_result(p.title or p.url, "취소됨", "")

    def _on_video_done(self, folder: Path) -> None:
        self._add_result(folder.name, "완료", str(folder))
        if self.auto_queue.isChecked():
            self.video_downloaded.emit(folder)

    def _on_finished_all(self, done: list) -> None:
        n = len(done)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.url_input.setEnabled(True)
        self.progress_label.setText(f"전체 완료 — 성공 {n}편")
        self.status_label.setText("")
        self.progress_bar.setValue(0)
        self.worker = None

    def _on_failed(self, err: str) -> None:
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.url_input.setEnabled(True)
        self.progress_label.setText("실패")
        self.status_label.setText(err)
        MessageBox("다운로드 실패", err, self).exec()
        self.worker = None

    def _add_result(self, title: str, status: str, location: str) -> None:
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        ts = datetime.now().strftime("%H:%M:%S")
        for col, text in enumerate([ts, title, status, location]):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self.result_table.setItem(row, col, item)
        self.result_table.scrollToBottom()
