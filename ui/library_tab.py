# -*- coding: utf-8 -*-
"""
ui/library_tab.py — 영상 라이브러리 탭.

data_root 안의 모든 <영상명>/ 폴더 스캔 → 분석 상태 / 편집점·완성본 개수 표로.
완성본 갤러리와 같은 패턴 (썸네일 + 표 + 우클릭 메뉴).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QHeaderView, QMenu,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, FluentIcon, InfoBar, InfoBarPosition, MessageBox,
    PushButton, TitleLabel,
)

from backend.config import AppConfig, all_videos_root


# 썸네일 사이즈 — 16:9 원본 그대로
THUMB_W = 144
THUMB_H = 81


# 분석 상태 enum (간단 dict)
STATUS_DONE = "_DONE"
STATUS_PARTIAL = "_PARTIAL"
STATUS_FAILED = "_FAILED"
STATUS_NONE = "NONE"          # 분석 폴더 없음 또는 마커 없음 (미분석)
STATUS_INPROGRESS = "INPROGRESS"   # 분석 폴더 있는데 마커 없음

STATUS_LABEL = {
    STATUS_DONE: ("✅ 완료", QColor(120, 200, 120)),
    STATUS_PARTIAL: ("⚠ 부분", QColor(220, 180, 80)),
    STATUS_FAILED: ("❌ 실패", QColor(220, 100, 100)),
    STATUS_NONE: ("⏳ 미분석", QColor(160, 160, 160)),
    STATUS_INPROGRESS: ("⏳ 진행중?", QColor(180, 180, 220)),
}


class LibraryThumbnailGenerator(QThread):
    """원본 영상 첫 프레임 → 썸네일 (background)."""

    thumb_ready = Signal(int, object)     # (row, thumb_path)
    finished_all = Signal()

    def __init__(self, items: list[dict]) -> None:
        super().__init__()
        self.items = list(items)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        for row, item in enumerate(self.items):
            if self._cancel:
                break
            video_mp4: Path | None = item.get("video_mp4")
            if not video_mp4 or not video_mp4.exists():
                continue
            thumb_path = video_mp4.parent / "_thumb.jpg"

            if not thumb_path.exists():
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-ss", "5",                # 5초 시점 (인트로 회피)
                            "-i", str(video_mp4),
                            "-vframes", "1",
                            "-vf", f"scale={THUMB_W}:-1",
                            str(thumb_path),
                            "-loglevel", "error",
                        ],
                        capture_output=True, timeout=15,
                    )
                except Exception:
                    continue

            if thumb_path.exists():
                self.thumb_ready.emit(row, thumb_path)

        self.finished_all.emit()


def _scan_video_folder(video_dir: Path) -> dict:
    """한 영상 폴더의 메타·상태 스캔.

    Returns:
        dict — video_basename, video_mp4, duration_s, source_uploader,
               status (STATUS_*), edit_plan_count, completed_count, mtime.
    """
    basename = video_dir.name

    # 원본 mp4 찾기
    originals = video_dir / "원본"
    video_mp4: Path | None = None
    if originals.exists():
        # <basename>.mp4 우선
        candidate = originals / f"{basename}.mp4"
        if candidate.exists():
            video_mp4 = candidate
        else:
            mp4s = sorted(originals.glob("*.mp4"))
            if mp4s:
                video_mp4 = mp4s[0]

    # source_meta.json 에서 uploader / duration
    uploader = ""
    duration_s = 0.0
    source_meta_path = originals / "source_meta.json" if originals.exists() else None
    if source_meta_path and source_meta_path.exists():
        try:
            with open(source_meta_path, encoding="utf-8") as f:
                sm = json.load(f)
            uploader = sm.get("uploader", "")
            duration_s = float(sm.get("duration_s", 0.0))
        except Exception:
            pass

    # 분석 상태
    analysis_dir = video_dir / "분석"
    if not analysis_dir.exists():
        status = STATUS_NONE
    elif (analysis_dir / STATUS_DONE).exists():
        status = STATUS_DONE
    elif (analysis_dir / STATUS_PARTIAL).exists():
        status = STATUS_PARTIAL
    elif (analysis_dir / STATUS_FAILED).exists():
        status = STATUS_FAILED
    else:
        status = STATUS_INPROGRESS

    # 편집점 / 완성본 개수
    edit_plan_count = 0
    edit_plans_dir = video_dir / "편집점"
    if edit_plans_dir.exists():
        edit_plan_count = sum(
            1 for d in edit_plans_dir.iterdir()
            if d.is_dir() and (d / "edit_plan.json").exists()
        )

    completed_count = 0
    completed_dir = video_dir / "완성"
    if completed_dir.exists():
        completed_count = sum(
            1 for d in completed_dir.iterdir()
            if d.is_dir() and (d / "full.mp4").exists()
        )

    # mtime — 폴더 자체
    try:
        mtime = video_dir.stat().st_mtime
    except OSError:
        mtime = 0.0

    return {
        "video_basename": basename,
        "video_dir": video_dir,
        "video_mp4": video_mp4,
        "duration_s": duration_s,
        "uploader": uploader,
        "status": status,
        "edit_plan_count": edit_plan_count,
        "completed_count": completed_count,
        "mtime": mtime,
    }


class LibraryTab(QWidget):
    """영상 라이브러리 탭 — 영상별 분석 상태 + 산출물 개수 한눈에."""

    # 영상을 분석 큐에 추가 시 발생 (analyze_tab 와 연결)
    analysis_requested = Signal(object)   # video_mp4: Path
    # 편집점 폴더를 편집 큐에 추가 시 발생 (edit_tab 와 연결)
    enqueue_edit_plan = Signal(object)    # plan_dir: Path

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setObjectName("libraryTab")
        self.config = config
        self.items: list[dict] = []
        self.thumb_thread: LibraryThumbnailGenerator | None = None

        self._setup_ui()
        self.refresh()

    # ------------------------------------------------------ UI ---

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # 헤더
        root.addWidget(TitleLabel("영상 라이브러리"))
        sub = BodyLabel(
            "yejjas 폴더의 모든 영상 한눈에. "
            "더블클릭 = Finder 보기 / 우클릭 = 분석 추가·열기·삭제."
        )
        sub.setStyleSheet("color: #666;")
        root.addWidget(sub)

        # 액션 row
        actions = QHBoxLayout()
        self.refresh_btn = PushButton(FluentIcon.SYNC, "새로고침")
        self.refresh_btn.clicked.connect(self.refresh)
        self.analyze_selected_btn = PushButton(FluentIcon.PLAY, "선택된 영상 분석 추가")
        self.analyze_selected_btn.clicked.connect(self._analyze_selected)
        self.open_root_btn = PushButton(FluentIcon.FOLDER, "yejjas 폴더 열기")
        self.open_root_btn.clicked.connect(self._open_root)
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.analyze_selected_btn)
        actions.addWidget(self.open_root_btn)
        actions.addStretch(1)
        self.count_label = BodyLabel("")
        self.count_label.setStyleSheet("color: #6B7280;")
        actions.addWidget(self.count_label)
        root.addLayout(actions)

        # 테이블
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "", "영상명", "출처", "길이", "분석 상태",
            "편집점", "완성본", "수정일",
        ])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, THUMB_W + 16)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setIconSize(QSize(THUMB_W, THUMB_H))
        self.table.verticalHeader().setDefaultSectionSize(THUMB_H + 8)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        root.addWidget(self.table, stretch=1)

    # ----------------------------------------------- 스캔/로드 ---

    def refresh(self) -> None:
        """data_root 의 모든 <영상명> 폴더 스캔."""
        if self.thumb_thread is not None:
            self.thumb_thread.cancel()
            self.thumb_thread.wait(2000)
            self.thumb_thread = None

        items: list[dict] = []
        data_root = all_videos_root(self.config)

        if data_root.exists():
            for video_dir in sorted(data_root.iterdir()):
                if not video_dir.is_dir() or video_dir.name.startswith("_"):
                    continue
                items.append(_scan_video_folder(video_dir))

        # 최근 수정순
        items.sort(key=lambda x: x["mtime"], reverse=True)
        self.items = items

        self._populate_table()

        # 상태 통계
        n_done = sum(1 for it in items if it["status"] == STATUS_DONE)
        n_undone = sum(1 for it in items if it["status"] == STATUS_NONE)
        n_partial = sum(
            1 for it in items
            if it["status"] in (STATUS_PARTIAL, STATUS_FAILED)
        )
        self.count_label.setText(
            f"총 {len(items)} 편 · 완료 {n_done} · 미분석 {n_undone} · 부분/실패 {n_partial}"
        )

        # 썸네일 비동기
        if items:
            self.thumb_thread = LibraryThumbnailGenerator(items)
            self.thumb_thread.thumb_ready.connect(self._set_thumb)
            self.thumb_thread.start()

    def _populate_table(self) -> None:
        self.table.setRowCount(len(self.items))
        for row, it in enumerate(self.items):
            # 0: 썸네일
            thumb_item = QTableWidgetItem()
            video_mp4 = it["video_mp4"]
            if video_mp4:
                thumb_path = video_mp4.parent / "_thumb.jpg"
                if thumb_path.exists():
                    thumb_item.setIcon(QIcon(QPixmap(str(thumb_path))))
            self.table.setItem(row, 0, thumb_item)

            # 1: 영상명 (잘림 + tooltip)
            basename = it["video_basename"]
            display = basename if len(basename) <= 50 else basename[:47] + "..."
            name_item = QTableWidgetItem(display)
            name_item.setToolTip(basename)
            self.table.setItem(row, 1, name_item)

            # 2: 출처 (uploader)
            self.table.setItem(row, 2, QTableWidgetItem(it["uploader"] or "-"))

            # 3: 길이
            d = it["duration_s"]
            if d > 0:
                mins = int(d // 60)
                secs = int(d % 60)
                duration_str = f"{mins}:{secs:02d}"
            else:
                duration_str = "-"
            self.table.setItem(row, 3, QTableWidgetItem(duration_str))

            # 4: 분석 상태 (라벨 + 색)
            status = it["status"]
            label, color = STATUS_LABEL.get(status, (status, QColor(0, 0, 0)))
            status_item = QTableWidgetItem(label)
            status_item.setForeground(color)
            self.table.setItem(row, 4, status_item)

            # 5: 편집점 개수
            ep = it["edit_plan_count"]
            self.table.setItem(row, 5, QTableWidgetItem(str(ep) if ep else "-"))

            # 6: 완성본 개수
            cp = it["completed_count"]
            self.table.setItem(row, 6, QTableWidgetItem(str(cp) if cp else "-"))

            # 7: 수정일
            try:
                mtime_str = datetime.fromtimestamp(it["mtime"]).strftime("%y-%m-%d")
            except (OSError, ValueError):
                mtime_str = "-"
            self.table.setItem(row, 7, QTableWidgetItem(mtime_str))

    def _set_thumb(self, row: int, thumb_path: Path) -> None:
        if row >= self.table.rowCount():
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        item.setIcon(QIcon(QPixmap(str(thumb_path))))

    # ----------------------------------------------- 액션 ---

    def _on_double_click(self, row: int, _col: int) -> None:
        """더블클릭 = Finder 에서 영상 폴더 열기."""
        if row < 0 or row >= len(self.items):
            return
        video_dir = self.items[row]["video_dir"]
        try:
            subprocess.run(["open", str(video_dir)], check=False)
        except Exception:
            pass

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.items):
            return
        item = self.items[row]

        menu = QMenu(self)
        analyze_act = menu.addAction("▶ 분석 큐에 추가")
        analyze_act.setEnabled(bool(item["video_mp4"]))

        # 코워크 명령어 — 분석 완료된 영상만 활성화
        cowork_act = menu.addAction("📋 후보 추천 명령어 복사 + Claude 띄우기")
        cowork_act.setEnabled(item["status"] in (STATUS_DONE, STATUS_PARTIAL))

        edit_plan_act = menu.addAction("📝 편집점 작성 명령어 복사 (후보 추천 + 자동 작성)")
        edit_plan_act.setEnabled(item["status"] in (STATUS_DONE, STATUS_PARTIAL))

        # ★ v1.6 — 편집점 있으면 편집 큐에 추가 서브메뉴 (전체 + 개별)
        edit_plans_dir = item["video_dir"] / "편집점"
        edit_plan_dirs: list[Path] = []
        if edit_plans_dir.exists():
            edit_plan_dirs = sorted([
                d for d in edit_plans_dir.iterdir()
                if d.is_dir() and (d / "edit_plan.json").exists()
            ])

        all_enqueue_act = None
        plan_enqueue_acts: list[tuple] = []   # (action, plan_dir)
        if edit_plan_dirs:
            submenu = menu.addMenu(f"📤 편집 큐에 추가 ({len(edit_plan_dirs)}개)")
            all_enqueue_act = submenu.addAction(
                f"⏏ 전체 추가 ({len(edit_plan_dirs)}개)"
            )
            submenu.addSeparator()
            for plan_dir in edit_plan_dirs:
                act = submenu.addAction(plan_dir.name)
                plan_enqueue_acts.append((act, plan_dir))

        menu.addSeparator()
        finder_act = menu.addAction("📁 Finder 에서 영상 폴더 보기")
        if item["video_mp4"]:
            video_show_act = menu.addAction("📁 원본 mp4 Finder 에서 보기")
        else:
            video_show_act = None
        menu.addSeparator()
        delete_act = menu.addAction("🗑 영상 폴더 삭제")

        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == analyze_act and item["video_mp4"]:
            self.analysis_requested.emit(item["video_mp4"])
            InfoBar.success(
                title="분석 큐에 추가됨",
                content=item["video_basename"][:50],
                position=InfoBarPosition.TOP,
                duration=2500, parent=self,
            )
        elif action == cowork_act:
            self._copy_cowork_command(item)
        elif action == edit_plan_act:
            self._copy_edit_plan_command(item)
        elif all_enqueue_act and action == all_enqueue_act:
            self._enqueue_edit_plans(edit_plan_dirs, label=f"전체 {len(edit_plan_dirs)}개")
        elif plan_enqueue_acts and any(action == a for a, _ in plan_enqueue_acts):
            for a, plan_dir in plan_enqueue_acts:
                if action == a:
                    self._enqueue_edit_plans([plan_dir], label=plan_dir.name)
                    break
        elif action == finder_act:
            subprocess.run(["open", str(item["video_dir"])], check=False)
        elif video_show_act and action == video_show_act:
            subprocess.run(["open", "-R", str(item["video_mp4"])], check=False)
        elif action == delete_act:
            self._confirm_delete(item)

    def _copy_cowork_command(self, item: dict) -> None:
        """분석 폴더 경로 + 후보 추천 명령어 → 클립보드 + Claude 활성화."""
        if item["status"] not in (STATUS_DONE, STATUS_PARTIAL):
            InfoBar.warning(
                title="복사 불가",
                content=f"분석 미완료 영상은 코워크에 보낼 수 없음 (현재: {item['status']})",
                position=InfoBarPosition.TOP,
                duration=3000, parent=self,
            )
            return

        analysis_path = item["video_dir"] / "분석"
        try:
            rel = analysis_path.relative_to(Path.home())
            display_path = f"~/{rel}"
        except ValueError:
            display_path = str(analysis_path)

        cmd = f"분석 폴더 {display_path}/ 보고 쇼츠 후보 추천해줘"
        QApplication.clipboard().setText(cmd)

        claude_ok = self._open_claude_app()
        msg = ("Claude 앱 띄움 — 새 채팅 → Cmd+V → Enter"
               if claude_ok else "Claude 앱 활성화 실패 — 클립보드만 복사됨")
        InfoBar.success(
            title="코워크 명령어 복사됨",
            content=msg,
            position=InfoBarPosition.TOP,
            duration=3500, parent=self,
        )

    def _copy_edit_plan_command(self, item: dict) -> None:
        """분석 폴더 + 편집점 폴더 경로 + 편집점 작성 명령어 → 클립보드 + Claude 활성화.

        후보 추천 + 점수 best 자동 선택 + 편집점 작성 한 번에 코워크에 던짐.
        """
        if item["status"] not in (STATUS_DONE, STATUS_PARTIAL):
            InfoBar.warning(
                title="복사 불가",
                content=f"분석 미완료 영상은 편집점 작성 불가 (현재: {item['status']})",
                position=InfoBarPosition.TOP,
                duration=3000, parent=self,
            )
            return

        analysis_path = item["video_dir"] / "분석"
        edit_plans_path = item["video_dir"] / "편집점"
        try:
            arel = analysis_path.relative_to(Path.home())
            erel = edit_plans_path.relative_to(Path.home())
            adisp = f"~/{arel}"
            edisp = f"~/{erel}"
        except ValueError:
            adisp = str(analysis_path)
            edisp = str(edit_plans_path)

        cmd = (
            f"다음 분석 폴더 보고 쇼츠 후보 추천 + 편집점 작성까지 한 번에 진행해줘:\n"
            f"  분석: {adisp}/\n"
            f"  편집점 저장: {edisp}/<YYMMDD_제목>/\n\n"
            f"흐름: 후보 5개 추천 (표 + 점수) → 가장 점수 높은 후보 1개 자동 선택 → "
            f"edit_plan.json + dialog.srt + explain.srt + meta.json + _READY 작성. "
            f"다른 후보로도 추가 작성 원하면 알려달라고 마지막에 묻기."
        )
        QApplication.clipboard().setText(cmd)

        claude_ok = self._open_claude_app()
        msg = ("Claude 앱 띄움 — 새 채팅 → Cmd+V → Enter"
               if claude_ok else "Claude 앱 활성화 실패 — 클립보드만 복사됨")
        InfoBar.success(
            title="편집점 작성 명령어 복사됨",
            content=msg,
            position=InfoBarPosition.TOP,
            duration=3500, parent=self,
        )

    def _enqueue_edit_plans(self, plan_dirs: list[Path], *, label: str) -> None:
        """편집점 폴더(들) → enqueue_edit_plan signal 발행 → edit_tab 큐 추가."""
        if not plan_dirs:
            return
        for p in plan_dirs:
            self.enqueue_edit_plan.emit(p)
        InfoBar.success(
            title="편집 큐에 추가됨",
            content=f"{label} → 편집 탭에서 시작 누르면 진행",
            position=InfoBarPosition.TOP,
            duration=3000, parent=self,
        )

    def _open_claude_app(self) -> bool:
        """Claude 데스크톱 앱 활성화 (analyze_tab 와 동일 패턴)."""
        try:
            r = subprocess.run(
                ["open", "-a", "Claude"],
                capture_output=True, text=True, timeout=3,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _analyze_selected(self) -> None:
        """선택된 영상들 (멀티 선택 OK) 일괄 분석 큐 추가."""
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not rows:
            InfoBar.warning(
                title="선택된 영상 없음",
                content="좌측 행을 클릭해서 선택 후 다시.",
                position=InfoBarPosition.TOP,
                duration=2500, parent=self,
            )
            return

        added = 0
        for row in rows:
            if row >= len(self.items):
                continue
            item = self.items[row]
            if item["video_mp4"]:
                self.analysis_requested.emit(item["video_mp4"])
                added += 1

        if added:
            InfoBar.success(
                title=f"{added}편 분석 큐에 추가",
                content="분석 탭에서 진행 확인.",
                position=InfoBarPosition.TOP,
                duration=3000, parent=self,
            )
        else:
            InfoBar.warning(
                title="추가 실패",
                content="원본 mp4 가 없는 영상은 분석 불가.",
                position=InfoBarPosition.TOP,
                duration=3000, parent=self,
            )

    def _confirm_delete(self, item: dict) -> None:
        box = MessageBox(
            "영상 폴더 삭제",
            f"'{item['video_basename']}' 폴더 통째 삭제할까요?\n"
            f"({item['video_dir']})\n\n"
            f"⚠ 원본·분석·편집점·완성본 모두 사라짐 (복구 불가)",
            self,
        )
        if box.exec():
            try:
                shutil.rmtree(item["video_dir"])
                InfoBar.success(
                    title="삭제됨",
                    content=item["video_basename"][:50],
                    position=InfoBarPosition.TOP,
                    duration=2500, parent=self,
                )
                self.refresh()
            except Exception as e:
                InfoBar.error(
                    title="삭제 실패",
                    content=str(e),
                    position=InfoBarPosition.TOP,
                    duration=4000, parent=self,
                )

    def _open_root(self) -> None:
        d = all_videos_root(self.config)
        d.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["open", str(d)], check=False)
        except Exception:
            pass
