# -*- coding: utf-8 -*-
"""
gui.py — 쇼돈 예짜 진입점.

run.command 또는 .app 의 launcher 가 venv python 으로 이 파일 실행.
"""

from __future__ import annotations

import sys
from pathlib import Path

# 직접 실행 (`python gui.py`) 시 패키지 import 경로 보정
if __package__ in (None, "") and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("쇼돈 예짜")
    app.setOrganizationName("showdon")
    app.setOrganizationDomain("showdon.com")

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
