# -*- coding: utf-8 -*-
"""
ui/edit_tab.py — 편집 탭.

편집점 폴더 선택 (단일 또는 다중) → ffmpeg 자동 편집 → 완성 폴더에 mp4.
PRD §5.1 와이어프레임 따름.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QButtonGroup, QFileDialog, QHBoxLayout, QHeaderView,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CheckBox, FluentIcon, InfoBar, InfoBarPosition, MessageBox,
    PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton, RadioButton,
    SubtitleLabel, TitleLabel,
)

from backend.config import AppConfig, completed_dir, edit_plans_dir
from backend.edit import EditProgressEvent
from backend.schema import EditPlan, load_dataclass
from .workers import EditWorker


# 인코딩 프리셋 (PRD 기본값 + 빠른 옵션)
ENCODING_PRESETS = [
    ("medium", 18, "medium · CRF 18 (권장 — 빠름·고품질)"),
    ("ultrafast", 23, "ultrafast · CRF 23 (매우 빠름·중간 품질)"),
    ("veryslow", 16, "veryslow · CRF 16 (느림·최고 품질)"),
]


class EditTab(QWidget):
    """편집 탭. 편집점 폴더 → 완성 폴더."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setObjectName("editTab")
        self.config = config

        # 큐: list of dict {plan_dir, name, status}
        self.queue: list[dict] = []
        self.worker: EditWorker | None = None

        self.setAcceptDrops(True)
        self._setup_ui()

    # ---------------------------------------------------------------- UI ----

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        root.addWidget(TitleLabel("자동 편집"))
        sub = BodyLabel(
            "코워크에서 만든 편집점 폴더를 선택하면 ffmpeg 으로 자동 편집.\n"
            "결과는 ~/showdon/yejjas/<채널>/완성/ 에 full.mp4 로 저장됩니다."
        )
        sub.setStyleSheet("color: #666;")
        root.addWidget(sub)

        # 편집점 추가
        add_row = QHBoxLayout()
        self.add_btn = PushButton(FluentIcon.FOLDER, "편집점 폴더 선택")
        self.add_btn.clicked.connect(self._on_add)
        self.add_multi_btn = PushButton(FluentIcon.FOLDER_ADD, "편집점 폴더 일괄")
        self.add_multi_btn.clicked.connect(self._on_add_multi)
        self.clear_btn = PushButton(FluentIcon.DELETE, "큐 비우기")
        self.clear_btn.clicked.connect(self._on_clear)
        add_row.addWidget(self.add_btn)
        add_row.addWidget(self.add_multi_btn)
        add_row.addStretch(1)
        add_row.addWidget(self.clear_btn)
        root.addLayout(add_row)

        drop_hint = BodyLabel("…또는 편집점 폴더를 드래그앤드롭")
        drop_hint.setStyleSheet("color: #999; font-style: italic;")
        root.addWidget(drop_hint)

        # 편집점 큐 테이블
        root.addWidget(SubtitleLabel("편집 큐"))
        self.queue_table = QTableWidget()
        self.queue_table.setColumnCount(4)
        self.queue_table.setHorizontalHeaderLabels(
            ["#", "편집점", "톤 / 길이", "상태"]
        )
        h = self.queue_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setMinimumHeight(120)
        self.queue_table.setMaximumHeight(180)
        # 행 클릭 시 — 실패 영상이면 에러 상세 다이얼로그
        self.queue_table.cellClicked.connect(self._on_queue_row_clicked)
        root.addWidget(self.queue_table)

        # v1.6 — Export 모드 선택
        mode_row = QHBoxLayout()
        mode_label = SubtitleLabel("Export 모드")
        mode_label.setMinimumWidth(140)
        mode_row.addWidget(mode_label)
        self.mode_group = QButtonGroup(self)
        self.mode_auto_radio = RadioButton("자동편집 (full.mp4)")
        self.mode_capcut_radio = RadioButton("CapCut Draft (수동 편집용)")
        self.mode_capcut_radio.setChecked(True)   # 디폴트: CapCut Draft
        self.mode_group.addButton(self.mode_auto_radio, 0)
        self.mode_group.addButton(self.mode_capcut_radio, 1)
        mode_row.addWidget(self.mode_auto_radio)
        mode_row.addWidget(self.mode_capcut_radio)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # 인코딩 프리셋 — 한 줄로 컴팩트
        preset_row = QHBoxLayout()
        preset_label = SubtitleLabel("인코딩 프리셋")
        preset_label.setMinimumWidth(140)
        preset_row.addWidget(preset_label)
        self.preset_group = QButtonGroup(self)
        self.preset_radios: list[tuple[RadioButton, str, int]] = []
        for i, (preset, crf, label) in enumerate(ENCODING_PRESETS):
            rb = RadioButton(label)
            if i == 0:
                rb.setChecked(True)
            self.preset_group.addButton(rb, i)
            self.preset_radios.append((rb, preset, crf))
            preset_row.addWidget(rb)
        preset_row.addStretch(1)
        root.addLayout(preset_row)

        # 출력 옵션 — 한 줄로 컴팩트
        out_row = QHBoxLayout()
        out_label = SubtitleLabel("출력 옵션")
        out_label.setMinimumWidth(140)
        out_row.addWidget(out_label)
        self.opt_full = CheckBox("full.mp4 (자막·템플릿)")
        self.opt_full.setChecked(True)
        self.opt_full_raw = CheckBox("full_raw.mp4 (자막 없음)")
        self.opt_full_raw.setChecked(True)
        self.opt_subcuts = CheckBox("cuts/ (sub-cut 별)")
        self.opt_subcuts.setChecked(True)
        out_row.addWidget(self.opt_full)
        out_row.addWidget(self.opt_full_raw)
        out_row.addWidget(self.opt_subcuts)
        out_row.addStretch(1)
        root.addLayout(out_row)

        # 진행
        self.current_label = BodyLabel("(대기 중)")
        self.current_label.setStyleSheet("font-weight: 500;")
        self.step_label = BodyLabel("")
        self.step_label.setStyleSheet("color: #666;")
        self.progress_bar = ProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        root.addWidget(self.current_label)
        root.addWidget(self.step_label)
        root.addWidget(self.progress_bar)

        # 로그 영역 (실패 원인 추적)
        root.addWidget(SubtitleLabel("로그"))
        self.log_area = PlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(140)
        self.log_area.setStyleSheet(
            "font-family: 'SF Mono', 'Menlo', monospace; font-size: 12px;"
        )
        root.addWidget(self.log_area, stretch=1)

        # 컨트롤
        ctrl_row = QHBoxLayout()
        self.open_dir_btn = PushButton(FluentIcon.FOLDER, "완성 폴더 열기")
        self.open_dir_btn.clicked.connect(self._open_completed_dir)
        ctrl_row.addWidget(self.open_dir_btn)
        ctrl_row.addStretch(1)

        self.start_btn = PrimaryPushButton(FluentIcon.PLAY, "편집 시작")
        self.start_btn.clicked.connect(self._on_start)
        ctrl_row.addWidget(self.start_btn)

        self.cancel_btn = PushButton(FluentIcon.CLOSE, "취소")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        ctrl_row.addWidget(self.cancel_btn)

        root.addLayout(ctrl_row)

    # --------------------------------------------------- 큐 관리 ---

    def _is_valid_plan_dir(self, p: Path) -> bool:
        return p.is_dir() and (p / "edit_plan.json").exists()

    def _enqueue(self, plan_dir: Path) -> bool:
        if not self._is_valid_plan_dir(plan_dir):
            return False
        for item in self.queue:
            if item["plan_dir"] == plan_dir:
                return False

        # edit_plan.json 메타 미리 읽어 톤/길이 표시
        tone_len = ""
        try:
            plan = load_dataclass(EditPlan, plan_dir / "edit_plan.json")
            tone_len = f"{plan.candidate.tone or '-'} / {plan.shorts.duration_s:.0f}초"
        except Exception:
            tone_len = "(메타 로드 실패)"

        self.queue.append({
            "plan_dir": plan_dir,
            "name": plan_dir.name,
            "tone_len": tone_len,
            "status": "대기",
        })
        self._refresh_table()
        return True

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                self._enqueue(p)

    def _on_add(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "편집점 폴더 선택", str(edit_plans_dir(self.config))
        )
        if d:
            self._enqueue(Path(d))

    def _on_add_multi(self) -> None:
        """편집점/ 부모 폴더 선택 → 안에 있는 모든 edit_plan.json 폴더 일괄 추가."""
        d = QFileDialog.getExistingDirectory(
            self, "편집점 부모 폴더 선택 (편집점/) ", str(edit_plans_dir(self.config))
        )
        if not d:
            return
        parent = Path(d)
        for sub in sorted(parent.iterdir()):
            if sub.is_dir() and (sub / "edit_plan.json").exists():
                self._enqueue(sub)

    def _on_clear(self) -> None:
        if self.worker:
            MessageBox(
                "큐 비우기",
                "편집 중에는 큐를 비울 수 없습니다.",
                self,
            ).exec()
            return
        self.queue.clear()
        self._refresh_table()

    # --------------------------------------------------- 편집 실행 ---

    def _on_start(self) -> None:
        pending = [item for item in self.queue if item["status"] == "대기"]
        if not pending:
            MessageBox("큐 비어있음", "대기 중인 편집점이 없습니다.", self).exec()
            return

        # 인코딩 프리셋 → config 적용
        idx = self.preset_group.checkedId()
        if 0 <= idx < len(self.preset_radios):
            _, preset, crf = self.preset_radios[idx]
            self.config.encoding_preset = preset
            self.config.encoding_crf = crf

        self._start_next(pending)

    def _start_next(self, pending: list[dict]) -> None:
        if not pending:
            self._on_all_done()
            return

        item = pending[0]
        item["status"] = "처리 중"
        self._refresh_table()

        plan_dir = item["plan_dir"]
        # plan_dir = ~/showdon/yejjas/<영상명>/편집점/<날짜_제목>/
        # 완성본 = ~/showdon/yejjas/<영상명>/완성/<날짜_제목>/
        # → 영상 부모 폴더 = plan_dir.parent.parent
        video_root = plan_dir.parent.parent
        out_dir = video_root / "완성" / plan_dir.name

        self.current_label.setText(plan_dir.name)
        self.step_label.setText("시작...")
        self.progress_bar.setValue(0)

        self._log(f"━━━ 편집 시작: {plan_dir.name} ━━━")
        self._log(f"  편집점: {plan_dir}")
        self._log(f"  출력:   {out_dir}")

        export_mode = "capcut" if self.mode_capcut_radio.isChecked() else "auto"
        self.worker = EditWorker(
            plan_dir, out_dir, config=self.config, export_mode=export_mode,
        )

        # 단계 전환 추적 (로그 중복 방지)
        last_step = {"name": None}

        def on_progress(evt: EditProgressEvent) -> None:
            self.step_label.setText(
                f"{evt.step}: {evt.message}" if evt.message else evt.step
            )
            if evt.percent > 0:
                # ★ v1.9.5 — 같은 정수면 setValue skip (macOS 26.x + Qt 6.11 paint pipeline 충돌 회피)
                new_val = int(evt.percent)
                if new_val != self.progress_bar.value():
                    self.progress_bar.setValue(new_val)
            # 단계 전환 시 로그 + 메시지 있으면 로그
            if evt.step != last_step["name"]:
                last_step["name"] = evt.step
                self._log(f"  ▶ {evt.step} 시작")
            if evt.message and evt.percent in (0.0, 100.0):
                self._log(f"    {evt.step}: {evt.message}")

        def on_completed(full_mp4: Path) -> None:
            item["status"] = "완료"
            item["full_mp4"] = full_mp4
            self._refresh_table()
            self._log(f"━━━ {plan_dir.name} 완료 → {full_mp4} ━━━")
            self.worker = None
            remaining = [i for i in self.queue if i["status"] == "대기"]
            self._start_next(remaining)

        def on_failed(err: str) -> None:
            item["status"] = "실패"
            item["error"] = err
            self._refresh_table()
            # 로그에 에러 풀 메시지 (ffmpeg stderr 포함)
            self._log(f"━━━ {plan_dir.name} 실패 ━━━")
            for line in err.splitlines():
                self._log(f"  ! {line}")
            # InfoBar 즉시 알림
            InfoBar.error(
                title="편집 실패",
                content=err.splitlines()[0][:200] if err else "알 수 없는 에러",
                position=InfoBarPosition.TOP,
                duration=8000,
                parent=self,
            )
            self.worker = None
            # 다음으로 계속
            remaining = [i for i in self.queue if i["status"] == "대기"]
            self._start_next(remaining)

        self.worker.progress.connect(on_progress)
        self.worker.completed.connect(on_completed)
        self.worker.failed.connect(on_failed)

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.add_btn.setEnabled(False)
        self.add_multi_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)

        self.worker.start()

    def _on_cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)

    def _on_all_done(self) -> None:
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.add_btn.setEnabled(True)
        self.add_multi_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)

        ok = sum(1 for i in self.queue if i["status"] == "완료")
        fail = sum(1 for i in self.queue if i["status"] == "실패")
        self.current_label.setText(f"전체 종료 — 완료 {ok}편 / 실패 {fail}편")
        self.step_label.setText("")
        self.progress_bar.setValue(0)

    def _open_completed_dir(self) -> None:
        d = completed_dir(self.config)
        d.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["open", str(d)], check=False)
        except Exception:
            pass

    def _on_queue_row_clicked(self, row: int, _col: int) -> None:
        """큐 행 클릭 — 실패 영상이면 에러 상세 모달."""
        if row < 0 or row >= len(self.queue):
            return
        item = self.queue[row]
        if item["status"] == "실패" and item.get("error"):
            box = MessageBox(
                f"편집 실패: {item['name']}",
                item["error"][:2000],
                self,
            )
            box.exec()

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_area.appendPlainText(f"[{ts}] {msg}")
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    # --------------------------------------------------- 테이블 ---

    def _refresh_table(self) -> None:
        self.queue_table.setRowCount(len(self.queue))
        for row, item in enumerate(self.queue):
            self.queue_table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            name_item = QTableWidgetItem(item["name"])
            if item.get("error"):
                name_item.setToolTip(item["error"])
            self.queue_table.setItem(row, 1, name_item)
            self.queue_table.setItem(row, 2, QTableWidgetItem(item.get("tone_len", "")))
            status_item = QTableWidgetItem(item["status"])
            if item["status"] == "실패":
                status_item.setForeground(Qt.GlobalColor.red)
            elif item["status"] == "완료":
                status_item.setForeground(Qt.GlobalColor.darkGreen)
            elif item["status"] == "처리 중":
                status_item.setForeground(Qt.GlobalColor.blue)
            self.queue_table.setItem(row, 3, status_item)
