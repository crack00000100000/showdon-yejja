# -*- coding: utf-8 -*-
"""
ui/caffeinate.py — macOS sleep 방지 헬퍼.

`caffeinate -d -i -m -s` 백그라운드 실행:
  -d: 디스플레이 슬립 차단
  -i: idle 슬립 차단
  -m: 디스크 마운트 슬립 차단
  -s: 시스템 슬립 차단 (전원 연결 시에만 유효)

큐 진행 중에만 활성화 (PRD §4.2 sleep_prevention=during_queue 정책 기본).
"""

from __future__ import annotations

import subprocess


class Caffeinate:
    """macOS caffeinate 프로세스 wrapper.

    사용 예:
        c = Caffeinate()
        c.start()       # 큐 시작 시
        ...
        c.stop()        # 큐 종료 시

    또는:
        with Caffeinate():
            # 이 블록 안에서는 시스템 슬립 차단
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(self) -> bool:
        """이미 활성화면 no-op. caffeinate 없는 OS 면 False 반환 (개발 환경 등)."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            self._proc = subprocess.Popen(
                ["caffeinate", "-d", "-i", "-m", "-s"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except FileNotFoundError:
            self._proc = None
            return False

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> "Caffeinate":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
