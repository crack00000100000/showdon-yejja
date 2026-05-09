# -*- coding: utf-8 -*-
"""
ui/analyze_tab.py — 분석 탭.

영상 큐 등록 → 백그라운드 순차 분석 → 단계별 진행률 + 풍부한 로그.
caffeinate 슬립 방지, 영상별 _DONE/_PARTIAL/_FAILED 마커.

다운로드 탭에서 video_downloaded signal 로 영상이 들어오면 자동으로 분석 시작.
이미 분석 중이면 큐에만 추가하고, 현재 영상 끝나면 다음 영상으로 자동 진행.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QHeaderView,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CheckBox, FluentIcon, InfoBar, InfoBarPosition,
    MessageBox, PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton,
    StrongBodyLabel, SubtitleLabel, TitleLabel,
)

from backend.analyze import AnalysisProgressEvent
from backend.config import AppConfig, all_videos_root, save_config
from backend.schema import StepStatus
from .caffeinate import Caffeinate
from .workers import AnalyzeWorker


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}


# 단계 한글 라벨
STEP_LABELS = {
    "ffprobe": "메타 추출",
    "stt": "음성 → 자막 (mlx-whisper)",
    "scene_detection": "장면 컷 검출",
    "frame_extraction": "프레임 추출",
    "ocr_candidates": "OCR 후보 시점",
    "ocr_local": "로컬 OCR (Apple Vision)",
    "face_clusters": "얼굴 검출",
}
STEP_ORDER = list(STEP_LABELS.keys())


# 단계별 예상 처리 시간 = 영상 길이 × 이 비율 (M4 Pro 기준, PRD §4.2 + 어덴덤)
# 첫 영상은 부정확, 진행 중에 실제 elapsed 로 자동 보정됨.
STEP_TIME_FACTORS: dict[str, float] = {
    "ffprobe": 0.001,
    "stt": 0.40,                # 가장 큰 비중 (faster-whisper large-v3 CPU)
    "scene_detection": 0.05,
    "frame_extraction": 0.05,
    "ocr_candidates": 0.02,
    "ocr_local": 0.20,          # PaddleOCR 더블체킹 활성 시
    "face_clusters": 0.05,
}


def _probe_duration(video_file: Path) -> float:
    """ffprobe 로 영상 길이 빠르게 가져오기 (~0.5초). 실패 시 0."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_file)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def _fmt_elapsed(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}초"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}분 {sec}초"
    h, m = divmod(m, 60)
    return f"{h}시간 {m}분 {sec}초"


class AnalyzeTab(QWidget):
    """분석 탭. 영상 큐 + 진행 상태 + 풍부한 로그."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setObjectName("analyzeTab")
        self.config = config
        self.worker: AnalyzeWorker | None = None
        self.caffeine = Caffeinate()

        # 큐: list of dict — {video_file, video_dir, name, status, error?}
        # video_file: 실제 mp4 파일
        # video_dir:  영상 부모 폴더 (= 분석/, 편집점/, 완성/ 가 만들어질 위치)
        self.queue: list[dict] = []

        # 진행 추적 (logging + ETA 계산용)
        self._video_start_time: float | None = None
        self._step_start_time: float | None = None
        self._current_step: str | None = None
        self._current_step_percent: float = 0.0
        self._current_step_skipped: bool = False
        self._current_video_path: Path | None = None
        self._video_duration_s: float = 0.0
        self._completed_steps: list[str] = []

        # UI 갱신용 (경과시간 1초마다)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

        self.setAcceptDrops(True)
        self._setup_ui()

    # ---------------------------------------------------------------- UI ----

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # 헤더
        root.addWidget(TitleLabel("영상 분석"))
        sub = BodyLabel(
            "영상을 큐에 등록하면 STT·장면·프레임·OCR·얼굴 자동 처리.\n"
            "다운로드 탭에서 받은 영상이 자동으로 큐에 들어오고, 비어있으면 자동 시작."
        )
        sub.setStyleSheet("color: #666;")
        root.addWidget(sub)

        # 영상 추가 (드래그&드롭 + 버튼)
        add_row = QHBoxLayout()
        self.add_files_btn = PushButton(FluentIcon.DOCUMENT, "파일 선택")
        self.add_files_btn.clicked.connect(self._on_add_files)
        self.add_folder_btn = PushButton(FluentIcon.FOLDER, "폴더 선택")
        self.add_folder_btn.clicked.connect(self._on_add_folder)
        self.clear_btn = PushButton(FluentIcon.DELETE, "큐 비우기")
        self.clear_btn.clicked.connect(self._on_clear)
        add_row.addWidget(self.add_files_btn)
        add_row.addWidget(self.add_folder_btn)
        add_row.addStretch(1)
        add_row.addWidget(self.clear_btn)
        root.addLayout(add_row)

        drop_hint = BodyLabel("…또는 영상을 이 창에 드래그앤드롭")
        drop_hint.setStyleSheet("color: #999; font-style: italic;")
        root.addWidget(drop_hint)

        # 큐 테이블
        root.addWidget(SubtitleLabel("큐"))
        queue_hint = BodyLabel(
            "💡 분석 완료된 영상 행 클릭 → 코워크 챗에 던질 명령어 자동 복사"
        )
        queue_hint.setStyleSheet("color: #6B7280; font-size: 12px;")
        root.addWidget(queue_hint)

        self.queue_table = QTableWidget()
        self.queue_table.setColumnCount(3)
        self.queue_table.setHorizontalHeaderLabels(["#", "영상", "상태"])
        h = self.queue_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setMinimumHeight(120)
        self.queue_table.setMaximumHeight(180)  # 큐 테이블은 컴팩트, 로그가 늘어남
        # 행 클릭 시 코워크 명령어 클립보드 복사 (분석 완료된 영상만)
        self.queue_table.cellClicked.connect(self._on_queue_row_clicked)
        root.addWidget(self.queue_table)

        # 현재 진행 — 영상명 prominent
        root.addWidget(SubtitleLabel("현재 진행"))

        self.current_label = BodyLabel("(대기 중)")
        font = QFont()
        font.setPointSize(15)
        font.setWeight(QFont.Weight.DemiBold)
        self.current_label.setFont(font)
        self.current_label.setStyleSheet("color: #1F2937;")
        root.addWidget(self.current_label)

        self.step_label = BodyLabel("")
        self.step_label.setStyleSheet("color: #4B5563;")
        root.addWidget(self.step_label)

        self.progress_bar = ProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        root.addWidget(self.progress_bar)

        self.elapsed_label = BodyLabel("")
        self.elapsed_label.setStyleSheet("color: #6B7280; font-size: 12px;")
        root.addWidget(self.elapsed_label)

        self.eta_label = BodyLabel("")
        self.eta_label.setStyleSheet("color: #6B7280; font-size: 12px;")
        root.addWidget(self.eta_label)

        # 로그 영역
        root.addWidget(SubtitleLabel("로그"))
        self.log_area = PlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(140)
        self.log_area.setStyleSheet(
            "font-family: 'SF Mono', 'Menlo', monospace; font-size: 12px;"
        )
        root.addWidget(self.log_area, stretch=1)

        # 슬립 방지 + 컨트롤
        ctrl_row = QHBoxLayout()
        self.sleep_check = CheckBox("분석 중 시스템 슬립 방지 (caffeinate)")
        self.sleep_check.setChecked(True)
        ctrl_row.addWidget(self.sleep_check)
        ctrl_row.addStretch(1)

        self.open_dir_btn = PushButton(FluentIcon.FOLDER, "분석 폴더 열기")
        self.open_dir_btn.clicked.connect(self._open_analysis_dir)
        ctrl_row.addWidget(self.open_dir_btn)

        self.start_btn = PrimaryPushButton(FluentIcon.PLAY, "분석 시작")
        self.start_btn.clicked.connect(self._on_start)
        ctrl_row.addWidget(self.start_btn)

        self.cancel_btn = PushButton(FluentIcon.CLOSE, "취소")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        ctrl_row.addWidget(self.cancel_btn)

        root.addLayout(ctrl_row)

        # 첫 로그
        self._log("준비 완료 — 영상을 큐에 등록하세요.")

    # ---------------------------------- 외부 API (탭간 연결, 자동 시작) ---

    def add_video(self, video_path_or_dir: Path | str, *, auto_start: bool = True) -> bool:
        """다운로드 탭 또는 외부에서 호출. 큐에 추가 + 분석 자동 시작.

        받을 수 있는 형태:
            - 영상 부모 폴더 (예: ~/showdon/yejjas/<영상명>/)
              → 안의 원본/video.mp4 를 찾아서 사용. 분석/ 도 같은 부모 아래.
            - 단일 mp4 파일
              → 영상 파일 자체. 부모 폴더는 영상 파일의 부모 디렉토리.

        auto_start=True 면 worker idle 일 때 자동으로 분석 시작.
        worker 가 이미 돌고 있으면 큐에만 추가 (worker 종료 후 자동으로 새 batch 시작).
        """
        p = Path(video_path_or_dir)
        if not p.exists():
            self._log(f"[추가 실패] 파일 없음: {p}")
            return False

        if p.is_dir():
            # 영상 부모 폴더 가정 — 원본/<폴더명>.mp4 또는 원본/video.mp4 (legacy) 찾기
            originals = p / "원본"
            video_file = originals / f"{p.name}.mp4"
            if not video_file.exists():
                legacy = originals / "video.mp4"
                if legacy.exists():
                    video_file = legacy
                else:
                    # 원본/ 안 또는 부모 안 직접 mp4
                    mp4s = (sorted(originals.glob("*.mp4")) if originals.exists()
                            else sorted(p.glob("*.mp4")))
                    if not mp4s:
                        self._log(f"[추가 실패] {p.name} 에 영상 파일 없음")
                        return False
                    video_file = mp4s[0]
            video_dir = p
        else:
            # 단일 mp4 파일
            if p.suffix.lower() not in VIDEO_EXTS:
                return False
            video_file = p
            # 영상 파일의 위치 추론:
            # 만약 .../<영상명>/원본/<영상명>.mp4 형태면 video_dir = .../<영상명>/
            # 아니면 video_dir = video_file.parent
            if p.parent.name == "원본" and p.parent.parent.exists():
                video_dir = p.parent.parent
            else:
                video_dir = p.parent

        added = self._enqueue(video_file, video_dir)

        if added and auto_start and self.worker is None:
            self._log("[자동 시작] 큐에 영상 들어옴 → 분석 시작")
            self._on_start()

        return added

    def _enqueue(self, video_file: Path, video_dir: Path) -> bool:
        for item in self.queue:
            if item["video_file"] == video_file:
                return False
        # 영상명은 video_dir 의 이름이 더 의미있음 (yymmdd-platform-title)
        display_name = video_dir.name if video_dir.name not in ("원본", "") else video_file.name
        self.queue.append({
            "video_file": video_file,
            "video_dir": video_dir,
            "name": display_name,
            "status": "대기",
            "error": None,
        })
        self._refresh_queue_table()
        self._log(f"[큐 추가] {display_name}")
        return True

    # ---------------------------------------------------- 드래그앤드롭 ---

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                for ext in VIDEO_EXTS:
                    for v in sorted(p.glob(f"*{ext}")):
                        self.add_video(v, auto_start=False)
            elif p.suffix.lower() in VIDEO_EXTS:
                self.add_video(p, auto_start=False)
        # 드롭 끝나면 한 번 시작 시도
        if self.worker is None and any(i["status"] == "대기" for i in self.queue):
            self._on_start()

    # ----------------------------------------------------- 핸들러 ---

    def _on_add_files(self) -> None:
        ext_filter = " ".join(f"*{e}" for e in VIDEO_EXTS)
        files, _ = QFileDialog.getOpenFileNames(
            self, "영상 파일 선택", str(Path.home()),
            f"영상 ({ext_filter})",
        )
        for f in files:
            self.add_video(Path(f), auto_start=False)
        if files and self.worker is None:
            self._on_start()

    def _on_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "영상 폴더 선택", str(Path.home())
        )
        if folder:
            p = Path(folder)
            for ext in VIDEO_EXTS:
                for v in sorted(p.rglob(f"*{ext}")):
                    self.add_video(v, auto_start=False)
            if self.worker is None and any(i["status"] == "대기" for i in self.queue):
                self._on_start()

    def _on_clear(self) -> None:
        if self.worker:
            MessageBox(
                "큐 비우기",
                "분석 중에는 큐를 비울 수 없습니다. 먼저 취소해주세요.",
                self,
            ).exec()
            return
        self.queue.clear()
        self._refresh_queue_table()
        self._log("[큐 비움]")

    def _on_start(self) -> None:
        pending = [item for item in self.queue if item["status"] == "대기"]
        if not pending:
            return  # 조용히 무시 (auto-start 시 호출되어도 깨끗하게)

        if self.worker is not None:
            self._log("[알림] 이미 분석 진행 중 — 큐에만 추가됨")
            return

        if self.sleep_check.isChecked():
            if self.caffeine.start():
                self._log("[caffeinate] 시스템 슬립 방지 활성화")

        # AnalyzeWorker 는 items dict 받음: {video_file, video_dir}
        items = [
            {"video_file": item["video_file"], "video_dir": item["video_dir"]}
            for item in pending
        ]

        self.worker = AnalyzeWorker(
            items,
            config=self.config,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.video_started.connect(self._on_video_started)
        self.worker.video_done.connect(self._on_video_done)
        self.worker.video_failed.connect(self._on_video_failed)
        self.worker.finished_all.connect(self._on_finished_all)

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.add_files_btn.setEnabled(False)
        self.add_folder_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)

        self._log(f"[배치 시작] 대기 영상 {len(items)}편")
        self.worker.start()

    def _on_cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.step_label.setText("취소 요청 — 현재 영상 종료 후 멈춥니다")
            self._log("[취소 요청] 현재 영상 종료 후 큐 중단")

    def _on_video_started(self, video_file: Path) -> None:
        self._video_start_time = time.time()
        self._current_video_path = video_file
        self._current_step = None
        self._step_start_time = None
        self._current_step_percent = 0.0
        self._completed_steps = []

        # 영상 길이 빠르게 추정 (~0.5초) — ETA 계산 기반
        self._video_duration_s = _probe_duration(video_file)

        # display name 은 큐 아이템에서 (video_dir.name = yymmdd-platform-title)
        display = video_file.name
        for item in self.queue:
            if item["video_file"] == video_file:
                display = item["name"]
                item["status"] = "처리 중"
                break

        self.current_label.setText(f"📺 {display}")
        self.step_label.setText("시작...")
        self.progress_bar.setValue(0)
        self.elapsed_label.setText("경과: 0초")

        # 영상 길이 알면 첫 ETA 추정값 띄우기
        if self._video_duration_s > 0:
            estimated = self._video_duration_s * self._total_factor()
            self.eta_label.setText(
                f"예상 분석 시간: ~{_fmt_elapsed(estimated)} (영상 {_fmt_elapsed(self._video_duration_s)})"
            )
        else:
            self.eta_label.setText("")

        self._refresh_queue_table()
        self._log(f"━━━ 분석 시작: {display} (영상 길이 {_fmt_elapsed(self._video_duration_s)}) ━━━")
        self._tick_timer.start()

    def _total_factor(self) -> float:
        """현재 config 기준 전체 단계 시간 비율 합."""
        factors = dict(STEP_TIME_FACTORS)
        if not self.config.enable_local_ocr:
            factors["ocr_local"] = 0
        return sum(factors.values())

    def _estimate_remaining(self) -> float | None:
        """남은 예상 시간 (초). 계산 불가면 None."""
        if self._video_duration_s <= 0 or self._video_start_time is None:
            return None

        factors = dict(STEP_TIME_FACTORS)
        if not self.config.enable_local_ocr:
            factors["ocr_local"] = 0
        total_factor = sum(factors.values())
        if total_factor <= 0:
            return None

        # 완료된 단계들의 비율 합
        done_ratio = sum(factors.get(s, 0) for s in self._completed_steps)
        # 현재 단계의 부분 진행
        if self._current_step:
            cur_factor = factors.get(self._current_step, 0)
            done_ratio += cur_factor * (self._current_step_percent / 100.0)

        progress = done_ratio / total_factor
        if progress <= 0.001:
            # 진행 거의 없으면 단순 추정값
            return self._video_duration_s * total_factor

        # 실제 elapsed / progress 로 전체 시간 추정 → 남은 시간
        actual_elapsed = time.time() - self._video_start_time
        estimated_total = actual_elapsed / progress
        return max(0.0, estimated_total - actual_elapsed)

    def _on_progress(self, evt: AnalysisProgressEvent) -> None:
        # 단계 전환 감지 — 새 단계로 진입했을 때 로그
        if evt.step != self._current_step:
            # 이전 단계 완료 — SKIPPED 면 완료 로그 생략 (이미 ⊘ 메시지 있음)
            if (self._current_step is not None
                and self._step_start_time is not None
                and not self._current_step_skipped):
                elapsed = time.time() - self._step_start_time
                prev_label = STEP_LABELS.get(self._current_step, self._current_step)
                self._log(f"  ✓ {prev_label} 완료 ({_fmt_elapsed(elapsed)})")
            if self._current_step is not None:
                self._completed_steps.append(self._current_step)

            self._current_step = evt.step
            self._current_step_percent = 0.0
            self._current_step_skipped = False
            self._step_start_time = time.time()

            label = STEP_LABELS.get(evt.step, evt.step)
            try:
                idx = STEP_ORDER.index(evt.step) + 1
                self._log(f"  ▶ [{idx}/{len(STEP_ORDER)}] {label} 시작")
            except ValueError:
                self._log(f"  ▶ {label} 시작")

        # 현재 단계 진행률 추적 (ETA 계산용)
        self._current_step_percent = max(self._current_step_percent, evt.percent or 0.0)

        # UI 라벨 갱신
        label = STEP_LABELS.get(evt.step, evt.step)
        try:
            idx = STEP_ORDER.index(evt.step) + 1
            label = f"[{idx}/{len(STEP_ORDER)}] {label}"
        except ValueError:
            pass

        if evt.message:
            self.step_label.setText(f"{label} — {evt.message}")
        else:
            self.step_label.setText(label)

        if evt.percent > 0:
            # ★ v1.9.5 — 같은 정수면 setValue skip (macOS 26.x + Qt 6.11 paint pipeline 충돌 회피)
            new_val = int(evt.percent)
            if new_val != self.progress_bar.value():
                self.progress_bar.setValue(new_val)

        # 단계 SKIPPED 면 로그 (1번만) + 다음 단계 전환 시 "완료" 로그 생략 마킹
        if evt.status == StepStatus.SKIPPED and not self._current_step_skipped:
            self._log(f"  ⊘ {label} SKIPPED — {evt.message}")
            self._current_step_skipped = True

    def _on_video_done(self, video_file: Path, meta) -> None:
        # 마지막 단계 완료 로그 — SKIPPED 면 생략
        if (self._current_step is not None
            and self._step_start_time is not None
            and not self._current_step_skipped):
            elapsed = time.time() - self._step_start_time
            prev_label = STEP_LABELS.get(self._current_step, self._current_step)
            self._log(f"  ✓ {prev_label} 완료 ({_fmt_elapsed(elapsed)})")

        status = "완료"
        if hasattr(meta, "analysis") and meta.analysis.status:
            s = (meta.analysis.status.value
                 if hasattr(meta.analysis.status, "value")
                 else str(meta.analysis.status))
            if s == "partial":
                status = "부분 완료"
            elif s == "completed":
                status = "완료"

        display = video_file.name
        for item in self.queue:
            if item["video_file"] == video_file:
                item["status"] = status
                display = item["name"]
                break
        self._refresh_queue_table()

        if self._video_start_time:
            elapsed = time.time() - self._video_start_time
            self._log(f"━━━ {display} {status} (총 {_fmt_elapsed(elapsed)}) ━━━")
        self._video_start_time = None
        self._current_step = None
        self._step_start_time = None

    def _on_video_failed(self, video_file: Path, err: str) -> None:
        display = video_file.name
        for item in self.queue:
            if item["video_file"] == video_file:
                item["status"] = "실패"
                item["error"] = err
                display = item["name"]
                break
        self._refresh_queue_table()
        self._log(f"━━━ {display} 실패 — {err} ━━━")
        self._video_start_time = None
        self._current_step = None
        self._step_start_time = None

    def _on_finished_all(self) -> None:
        self._tick_timer.stop()
        self.caffeine.stop()
        self._log("[caffeinate] 슬립 방지 비활성화")

        completed = sum(1 for i in self.queue if i["status"] in ("완료", "부분 완료"))
        failed = sum(1 for i in self.queue if i["status"] == "실패")
        self.current_label.setText(
            f"전체 종료 — 완료 {completed}편 / 실패 {failed}편"
        )
        self.step_label.setText("")
        self.progress_bar.setValue(0)
        self.elapsed_label.setText("")
        self.eta_label.setText("")
        self.worker = None

        self._log(f"[배치 종료] 완료 {completed}편 / 실패 {failed}편")

        # ★ 작업 중에 새로 추가된 영상 있으면 자동으로 다음 batch 시작
        new_pending = [item for item in self.queue if item["status"] == "대기"]
        if new_pending:
            self._log(f"[다음 batch] 새로 들어온 영상 {len(new_pending)}편 — 자동 시작")
            QTimer.singleShot(800, self._on_start)
        else:
            self.start_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.add_files_btn.setEnabled(True)
            self.add_folder_btn.setEnabled(True)
            self.clear_btn.setEnabled(True)

    def _on_tick(self) -> None:
        """1초마다 경과 시간 + ETA 갱신."""
        if self._video_start_time is None:
            return
        elapsed = time.time() - self._video_start_time
        self.elapsed_label.setText(f"경과: {_fmt_elapsed(elapsed)}")

        # ETA 계산 + 표시
        remaining = self._estimate_remaining()
        if remaining is not None and remaining > 0:
            done_at = datetime.now().timestamp() + remaining
            done_str = datetime.fromtimestamp(done_at).strftime("%H:%M")
            self.eta_label.setText(
                f"남은 시간: ~{_fmt_elapsed(remaining)}  ·  완료 예정 {done_str}"
            )

    def _on_queue_row_clicked(self, row: int, _col: int) -> None:
        """큐 행 클릭 → 분석 완료된 영상이면 코워크 트리거.

        클립보드 복사 + Claude 활성화 + InfoBar 알림.
        """
        if row < 0 or row >= len(self.queue):
            return
        item = self.queue[row]

        # 분석 완료된 영상만 (대기/처리 중/실패는 의미 없음)
        if item["status"] not in ("완료", "부분 완료"):
            InfoBar.warning(
                title="아직 분석 안 끝남",
                content=f"상태: {item['status']} — 완료된 영상만 복사 가능",
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        claude_ok = self._trigger_cowork_for_item(item, show_notification=False)
        msg = ("Claude 앱 띄움 — 새 채팅 → Cmd+V → Enter"
               if claude_ok else "Claude 앱 활성화 실패 — 클립보드만 복사됨")
        InfoBar.success(
            title="코워크 명령어 복사됨",
            content=msg,
            position=InfoBarPosition.TOP,
            duration=3500,
            parent=self,
        )

    def _trigger_cowork_for_item(self, item: dict, *, show_notification: bool = False) -> bool:
        """영상 1편의 코워크 명령어 → 클립보드 복사 + Claude 활성화 + (옵션) macOS 알림.

        Returns:
            Claude 앱 활성화 성공 여부
        """
        video_dir: Path = item["video_dir"]
        analysis_path = video_dir / "분석"
        try:
            rel = analysis_path.relative_to(Path.home())
            display_path = f"~/{rel}"
        except ValueError:
            display_path = str(analysis_path)

        cmd = f"분석 폴더 {display_path}/ 보고 쇼츠 후보 추천해줘"
        QApplication.clipboard().setText(cmd)
        claude_ok = self._open_claude_app()

        if show_notification:
            self._post_macos_notification(
                title="쇼돈 예짜 — 분석 완료",
                subtitle=item["name"][:80],
                body="Claude 띄움 · Cmd+V → Enter",
            )

        self._log(f"[코워크 트리거] {item['name']}")
        return claude_ok

    def _post_macos_notification(self, *, title: str, subtitle: str, body: str) -> None:
        """macOS Notification Center 알림 (osascript)."""
        # 따옴표 escape — AppleScript 문자열은 \" 안전 처리
        def _esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'display notification "{_esc(body)}" '
            f'with title "{_esc(title)}" '
            f'subtitle "{_esc(subtitle)}"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    def _open_claude_app(self) -> bool:
        """Claude 데스크톱 앱 활성화 (앞으로 가져옴). 성공 여부 반환.

        macOS `open -a Claude` 로 활성화. 앱 안 떠있으면 실행되고, 떠있으면 앞으로.
        새 채팅·명령어 입력·Enter 는 종운님이 직접 (Claude 앱이 외부 자동화 API 미제공).
        """
        try:
            r = subprocess.run(
                ["open", "-a", "Claude"],
                capture_output=True, text=True, timeout=3,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _open_analysis_dir(self) -> None:
        # data_root 열어서 사용자가 영상 폴더들 직접 둘러볼 수 있게
        d = all_videos_root(self.config)
        d.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["open", str(d)], check=False)
        except Exception:
            pass

    # --------------------------------------------------- 로그 헬퍼 ---

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_area.appendPlainText(f"[{ts}] {msg}")
        # 자동 스크롤
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------- 큐 테이블 갱신 ---

    def _refresh_queue_table(self) -> None:
        self.queue_table.setRowCount(len(self.queue))
        for row, item in enumerate(self.queue):
            self.queue_table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            name_item = QTableWidgetItem(item["name"])
            if item.get("error"):
                name_item.setToolTip(item["error"])
            self.queue_table.setItem(row, 1, name_item)
            status_item = QTableWidgetItem(item["status"])
            if item["status"] == "실패":
                status_item.setForeground(Qt.GlobalColor.red)
            elif item["status"] == "부분 완료":
                status_item.setForeground(Qt.GlobalColor.darkYellow)
            elif item["status"] == "완료":
                status_item.setForeground(Qt.GlobalColor.darkGreen)
            elif item["status"] == "처리 중":
                status_item.setForeground(Qt.GlobalColor.blue)
            self.queue_table.setItem(row, 2, status_item)
