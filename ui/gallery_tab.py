# -*- coding: utf-8 -*-
"""
ui/gallery_tab.py — 완성본 갤러리 탭.

data_root 안의 모든 <영상명>/완성/<날짜_제목>/full.mp4 를 한눈에 보여줌.
썸네일은 background thread 로 ffmpeg 호출해 첫 프레임 캐시.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QMenu,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, FluentIcon, InfoBar, InfoBarPosition, MessageBox,
    PushButton, TitleLabel,
)

from backend.config import AppConfig, all_videos_root


# 썸네일 사이즈 — 9:16 영상이라 세로형
THUMB_W = 80
THUMB_H = 144


class ThumbnailGenerator(QThread):
    """ffmpeg 으로 첫 프레임 추출. 영상별 비동기 처리, 끝나는 대로 signal."""

    thumb_ready = Signal(int, object)        # (row, thumb_path: Path)
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
            full_mp4: Path = item["full_mp4"]
            thumb_path = full_mp4.parent / "_thumb.jpg"

            if not thumb_path.exists():
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-ss", "1",
                            "-i", str(full_mp4),
                            "-vframes", "1",
                            "-vf", f"scale={THUMB_W}:-1",
                            str(thumb_path),
                            "-loglevel", "error",
                        ],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    continue

            if thumb_path.exists():
                self.thumb_ready.emit(row, thumb_path)

        self.finished_all.emit()


class GalleryTab(QWidget):
    """완성본 갤러리 탭."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setObjectName("galleryTab")
        self.config = config
        self.items: list[dict] = []
        self.thumb_thread: ThumbnailGenerator | None = None

        self._setup_ui()
        # 초기 로드는 lazy — 첫 탭 활성화 시
        # 단순화: init 에서 한 번 로드
        self.refresh()

    # ------------------------------------------------------ UI ---

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # 헤더
        root.addWidget(TitleLabel("완성본 갤러리"))
        sub = BodyLabel(
            "모든 영상의 완성된 쇼츠 한눈에. "
            "더블클릭 = 재생 / 우클릭 = Finder 보기·삭제."
        )
        sub.setStyleSheet("color: #666;")
        root.addWidget(sub)

        # 액션 row
        actions = QHBoxLayout()
        self.refresh_btn = PushButton(FluentIcon.SYNC, "새로고침")
        self.refresh_btn.clicked.connect(self.refresh)
        self.open_root_btn = PushButton(FluentIcon.FOLDER, "yejjas 폴더 열기")
        self.open_root_btn.clicked.connect(self._open_root)
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.open_root_btn)
        actions.addStretch(1)
        self.count_label = BodyLabel("")
        self.count_label.setStyleSheet("color: #6B7280;")
        actions.addWidget(self.count_label)
        root.addLayout(actions)

        # 테이블
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["", "제목", "원본 영상", "톤 / 길이", "생성일", "경로"]
        )
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, THUMB_W + 16)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setAlternatingRowColors(True)
        self.table.setIconSize(QSize(THUMB_W, THUMB_H))
        self.table.verticalHeader().setDefaultSectionSize(THUMB_H + 8)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        root.addWidget(self.table, stretch=1)

    # ----------------------------------------------- 스캔/로드 ---

    def refresh(self) -> None:
        """data_root 스캔 → 모든 완성본 찾고 표 갱신 + 썸네일 비동기 생성."""
        # 진행 중인 thumb thread 중단
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
                completed = video_dir / "완성"
                if not completed.exists():
                    continue
                for plan_dir in sorted(completed.iterdir()):
                    if not plan_dir.is_dir():
                        continue
                    full_mp4 = plan_dir / "full.mp4"
                    if not full_mp4.exists():
                        continue

                    meta_info: dict = {}
                    meta_path = plan_dir / "meta.json"
                    if meta_path.exists():
                        try:
                            with open(meta_path, encoding="utf-8") as f:
                                meta_info = json.load(f)
                        except Exception:
                            pass

                    items.append({
                        "video_basename": video_dir.name,
                        "plan_dir": plan_dir,
                        "full_mp4": full_mp4,
                        "meta": meta_info,
                        "mtime": full_mp4.stat().st_mtime,
                    })

        # 최신순
        items.sort(key=lambda x: x["mtime"], reverse=True)
        self.items = items

        self._populate_table()
        self.count_label.setText(f"총 {len(items)} 편")

        # 썸네일 비동기 생성
        if items:
            self.thumb_thread = ThumbnailGenerator(items)
            self.thumb_thread.thumb_ready.connect(self._set_thumb)
            self.thumb_thread.start()

    def _populate_table(self) -> None:
        self.table.setRowCount(len(self.items))
        for row, it in enumerate(self.items):
            meta = it["meta"]

            # 0: 썸네일 (있으면 즉시, 없으면 background 가 채움)
            thumb_item = QTableWidgetItem()
            thumb_path = it["full_mp4"].parent / "_thumb.jpg"
            if thumb_path.exists():
                thumb_item.setIcon(QIcon(QPixmap(str(thumb_path))))
            self.table.setItem(row, 0, thumb_item)

            # 1: 제목
            title = meta.get("title", it["plan_dir"].name)
            title_item = QTableWidgetItem(str(title))
            title_item.setToolTip(str(title))
            self.table.setItem(row, 1, title_item)

            # 2: 원본 영상 (basename 줄임)
            basename = it["video_basename"]
            display_basename = basename if len(basename) <= 50 else basename[:47] + "..."
            video_item = QTableWidgetItem(display_basename)
            video_item.setToolTip(basename)
            self.table.setItem(row, 2, video_item)

            # 3: 톤 / 길이
            tone = meta.get("tone", "?")
            duration = meta.get("duration_s") or 0
            tone_len = (
                f"{tone} / {int(duration)}초"
                if duration else str(tone)
            )
            self.table.setItem(row, 3, QTableWidgetItem(tone_len))

            # 4: 생성일
            mtime_str = datetime.fromtimestamp(it["mtime"]).strftime(
                "%y-%m-%d %H:%M"
            )
            self.table.setItem(row, 4, QTableWidgetItem(mtime_str))

            # 5: 경로 (~ 표기)
            try:
                rel = it["full_mp4"].relative_to(Path.home())
                path_str = f"~/{rel}"
            except ValueError:
                path_str = str(it["full_mp4"])
            path_item = QTableWidgetItem(path_str)
            path_item.setToolTip(str(it["full_mp4"]))
            self.table.setItem(row, 5, path_item)

    def _set_thumb(self, row: int, thumb_path: Path) -> None:
        """ThumbnailGenerator 가 완료한 항목에 아이콘 박기."""
        if row >= self.table.rowCount():
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        item.setIcon(QIcon(QPixmap(str(thumb_path))))

    # ----------------------------------------------- 액션 ---

    def _on_double_click(self, row: int, _col: int) -> None:
        """더블클릭 = 기본 플레이어로 mp4 재생."""
        if row < 0 or row >= len(self.items):
            return
        full_mp4 = self.items[row]["full_mp4"]
        try:
            subprocess.run(["open", str(full_mp4)], check=False)
        except Exception:
            pass

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.items):
            return
        item = self.items[row]

        menu = QMenu(self)
        play_act = menu.addAction("▶ 재생")
        finder_act = menu.addAction("📁 Finder 에서 보기")
        plan_act = menu.addAction("📁 편집점 폴더 열기")
        menu.addSeparator()
        delete_act = menu.addAction("🗑 완성본 삭제")

        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == play_act:
            subprocess.run(["open", str(item["full_mp4"])], check=False)
        elif action == finder_act:
            subprocess.run(["open", "-R", str(item["full_mp4"])], check=False)
        elif action == plan_act:
            # 같은 이름의 편집점 폴더 (있으면)
            video_root = item["plan_dir"].parent.parent
            edit_plan = video_root / "편집점" / item["plan_dir"].name
            target = edit_plan if edit_plan.exists() else item["plan_dir"]
            subprocess.run(["open", str(target)], check=False)
        elif action == delete_act:
            self._confirm_delete(item)

    def _confirm_delete(self, item: dict) -> None:
        box = MessageBox(
            "완성본 삭제",
            f"'{item['plan_dir'].name}' 의 완성본 폴더 통째로 삭제할까요?\n\n"
            f"({item['plan_dir']})\n\n"
            f"(편집점 폴더는 그대로 남고 — 다시 편집 가능)",
            self,
        )
        if box.exec():
            try:
                shutil.rmtree(item["plan_dir"])
                InfoBar.success(
                    title="삭제됨",
                    content=item["plan_dir"].name,
                    position=InfoBarPosition.TOP,
                    duration=2500,
                    parent=self,
                )
                self.refresh()
            except Exception as e:
                InfoBar.error(
                    title="삭제 실패",
                    content=str(e),
                    position=InfoBarPosition.TOP,
                    duration=4000,
                    parent=self,
                )

    def _open_root(self) -> None:
        d = all_videos_root(self.config)
        d.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["open", str(d)], check=False)
        except Exception:
            pass
