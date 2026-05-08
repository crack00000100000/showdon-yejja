# -*- coding: utf-8 -*-
"""
ui/main_window.py — qfluentwidgets FluentWindow.

3 탭 구조: 다운로드 / 분석 / 편집.
다운로드 탭의 video_downloaded Signal → 분석 탭의 add_video 자동 연결.

macOS traffic lights 좌측 배치:
  qframelesswindow 의 hook 메서드 systemTitleBarRect() 오버라이드.
  좌측 92px 영역을 macOS 시스템에 양보 → 거기에 NSWindow native traffic lights 그려짐.
  (showdon-downloader 가 검증한 패턴)
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from qfluentwidgets import FluentIcon, FluentWindow

from backend.config import VERSION, load_config

from .analyze_tab import AnalyzeTab
from .download_tab import DownloadTab
from .edit_tab import EditTab
from .gallery_tab import GalleryTab
from .library_tab import LibraryTab


# 앱 아이콘 (저장소 루트의 design/icon.svg)
_APP_ICON_PATH = Path(__file__).resolve().parent.parent / "design" / "icon.svg"


# =============================================================================
# 윈도우 사이즈 상수 (CLAUDE.md 패턴)
# =============================================================================

DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 1040
MIN_WIDTH = 900
MIN_HEIGHT = 880
MAX_REASONABLE_WIDTH = 4000
MAX_REASONABLE_HEIGHT = 3000


class MainWindow(FluentWindow):
    # macOS traffic lights 가 차지할 좌측 영역 폭 (78px traffic lights + 14px 여유)
    _TITLE_BAR_LEFT_PAD = 92

    def __init__(self) -> None:
        super().__init__()

        self.config = load_config()

        # 5 탭
        self.download_tab = DownloadTab(self.config)
        self.library_tab = LibraryTab(self.config)
        self.analyze_tab = AnalyzeTab(self.config)
        self.edit_tab = EditTab(self.config)
        self.gallery_tab = GalleryTab(self.config)

        # 다운로드 → 분석 자동 큐 연결
        self.download_tab.video_downloaded.connect(self.analyze_tab.add_video)
        # 라이브러리 → 분석 자동 큐 연결 (선택된 영상 분석)
        self.library_tab.analysis_requested.connect(self.analyze_tab.add_video)
        # 라이브러리 → 편집 자동 큐 연결 (편집점 폴더 추가)
        self.library_tab.enqueue_edit_plan.connect(self.edit_tab._enqueue)

        # FluentWindow navigation
        self.addSubInterface(self.download_tab, FluentIcon.DOWNLOAD, "다운로드")
        self.addSubInterface(self.library_tab, FluentIcon.LIBRARY, "라이브러리")
        self.addSubInterface(self.analyze_tab, FluentIcon.SEARCH, "분석")
        self.addSubInterface(self.edit_tab, FluentIcon.EDIT, "편집")
        self.addSubInterface(self.gallery_tab, FluentIcon.MOVIE, "갤러리")

        # 뒤로가기 버튼 숨김 + macOS traffic lights 와 겹침 회피
        self._adjust_navigation_for_macos()

        # 윈도우 메타
        self.setWindowTitle(f"쇼돈 예짜 v{VERSION}")
        if _APP_ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(_APP_ICON_PATH)))
        self._setup_window_geometry()

    def _adjust_navigation_for_macos(self) -> None:
        """macOS traffic lights 와 panel 이 겹치지 않게 정리.

        1) 뒤로가기 버튼 숨김 — 단일 윈도우 앱이라 navigation history 불필요
        2) panel 상단 padding 추가 — 햄버거 메뉴(menuButton) 가 traffic lights 아래로
        3) titleBar 위치 조정은 _reposition_title_bar() + resizeEvent 에서 처리
        """
        nav = getattr(self, "navigationInterface", None)
        if nav is not None:
            panel = getattr(nav, "panel", None)
            if panel is not None:
                # 뒤로가기 버튼 숨김
                return_btn = getattr(panel, "returnButton", None)
                if return_btn is not None:
                    return_btn.setVisible(False)
                    return_btn.setFixedHeight(0)
                # 패널 상단 padding (햄버거 메뉴 위치 ↓)
                layout = panel.layout()
                if layout is not None:
                    margins = layout.contentsMargins()
                    layout.setContentsMargins(
                        margins.left(), 36,
                        margins.right(), margins.bottom(),
                    )

        # titleBar 자체를 우측으로 이동 (다운로더/리무버 패턴)
        self._reposition_title_bar()

    def _reposition_title_bar(self) -> None:
        """titleBar 를 traffic lights 영역만큼 우측으로 이동 + 폭 조정.

        다운로더·리무버 검증된 패턴. nav panel 살아있는 우리 케이스에서도
        titleBar 가 nav panel 우측에 자연스럽게 위치하면서 traffic lights 와
        같은 시작 라인에 align 됨.
        """
        tb = getattr(self, "titleBar", None)
        if tb is None:
            return
        tb.move(self._TITLE_BAR_LEFT_PAD, 0)
        tb.resize(
            max(self.width() - self._TITLE_BAR_LEFT_PAD, 0),
            tb.height(),
        )

    def resizeEvent(self, event) -> None:
        """FluentWindow.resizeEvent 가 titleBar 위치를 되돌리므로 매번 재배치."""
        super().resizeEvent(event)
        self._reposition_title_bar()

    def systemTitleBarRect(self, size: QSize) -> QRect:
        """qframelesswindow hook — macOS 에서 traffic lights 가 그려질 좌측 영역 정의.

        이 rect 안의 영역을 macOS 시스템에 양보하면 NSWindow 가 거기에
        native traffic lights (close/min/max) 를 그린다.

        오버라이드 안 하면 디폴트 동작이 우측 영역을 양보 → emulated 버튼이 우측에.
        showdon-downloader 가 검증한 패턴.
        """
        return QRect(
            0,
            0 if self.isFullScreen() else 9,
            self._TITLE_BAR_LEFT_PAD,
            size.height(),
        )

    def _setup_window_geometry(self) -> None:
        """저장된 위치·크기 복원. 비정상 값이면 reset (CLAUDE.md sanity check)."""
        # TODO: QSettings 영속화 — v1.1
        w, h = DEFAULT_WIDTH, DEFAULT_HEIGHT
        if not (MIN_WIDTH <= w <= MAX_REASONABLE_WIDTH):
            w = DEFAULT_WIDTH
        if not (MIN_HEIGHT <= h <= MAX_REASONABLE_HEIGHT):
            h = DEFAULT_HEIGHT
        self.resize(w, h)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
