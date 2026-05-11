# -*- coding: utf-8 -*-
"""
yt-dlp 브라우저 쿠키 fallback 체인.

배경
----
YouTube 가 2024년 후반부터 비로그인 클라이언트의 video info 요청을
점점 더 공격적으로 차단해 "Sign in to confirm you're not a bot" 페이지를
띄움. yt-dlp 자체 버그가 아니라 YouTube 서버 정책 — 우회 방법은
사용자 브라우저의 쿠키를 `cookiesfrombrowser` 로 넘기는 것.

본 모듈은 두 가지를 제공:

1. ``BROWSER_CHAIN`` — 우선순위 (Chrome → Safari → Brave). v0.4.0 결정.
2. ``CookieBrowserPicker`` — 한 다운로드 세션 동안 작동하는 브라우저를
   기억해 두고, 실패 시 다음 브라우저로 자동 fallback 하는 헬퍼.

특히 .app 번들에서 macOS TCC 가 브라우저 쿠키 DB 접근을 막을 수 있어
첫 호출 시 권한 다이얼로그가 뜰 수 있음 (Safari/Chrome 의 ~/Library 접근).
브라우저가 설치 안 됐거나 권한 거부 시 다음 브라우저로 자동 패스.
"""

from typing import Callable, Iterable, List, Optional, Tuple

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


# 사용자 선택: 크롬 → 사파리 → 브레이브 (v0.4.0)
BROWSER_CHAIN: Tuple[str, ...] = ("chrome", "safari", "brave")

BROWSER_LABELS = {
    "chrome": "Chrome",
    "safari": "Safari",
    "brave": "Brave",
    "firefox": "Firefox",
    "edge": "Edge",
    "arc": "Arc",
}


# YouTube 봇 감지 우회용 player_client 다양화.
#
# ★ v0.4.1 — 쿠키가 있으면 yt-dlp 기본 클라이언트를 그대로 쓴다 (full format 리스트
# 확보 — H.264 mp4 매칭 가능). player_client 다양화는 쿠키 없는 마지막 fallback
# 에서만 적용.
#
# ★ v0.4.6 — `tv_simply` 를 맨 앞에 추가. 일부 영상이 mweb/tv 클라이언트에선
# DRM-only 로 응답 (다른 Mac 에서 "This video is DRM protected" 발생 케이스).
# tv_simply 는 yt-dlp 가 DRM 회피용으로 권장하는 TV 클라이언트로, 동일 영상에
# 대해 정상 (non-DRM) format 을 노출함. POT 요구도 가장 적음.
_YOUTUBE_NOCOOKIES_EXTRACTOR_ARGS = {
    "youtube": {
        "player_client": ["tv_simply", "mweb", "web"],
    },
}


# ----------------------------------------------------------------------------
# 에러 패턴 — 다음 브라우저로 fallback 해야 하는 케이스
# ----------------------------------------------------------------------------
# YouTube 봇 감지 / 권한 / 쿠키 DB 접근 실패 패턴.
# 이 외의 에러(URL 무효, 영상 삭제 등)는 fallback 무의미 → 즉시 raise.
_RECOVERABLE_PATTERNS = (
    # YouTube 봇 감지
    "sign in to confirm",
    "not a bot",
    "confirm you're not",
    "confirm you are not",
    # 쿠키 자체 문제
    "cookies-from-browser",
    "could not find",
    "cookie database",
    "no cookies",
    # 권한 / 접근 거부 (TCC, 403 등)
    "permission denied",
    "http error 403",
    "forbidden",
    "operation not permitted",
    # ★ v0.4.3 — format 매칭 실패도 다른 브라우저로 재시도해볼 가치 있음.
    # 배경: .app 실행 시 macOS Keychain 접근 권한 부족으로 Chrome 의 암호화된
    # auth 쿠키를 풀지 못 함 → YouTube 가 limited format 리스트만 줘서 format
    # selector 가 매칭 실패. Safari 는 keychain 안 거쳐서 정상 동작 가능성 있음.
    "requested format is not available",
)


def is_recoverable_error(err_str: str) -> bool:
    """이 에러 메시지면 다음 브라우저로 fallback 해 볼 가치가 있다."""
    if not err_str:
        return False
    s = err_str.lower()
    return any(p in s for p in _RECOVERABLE_PATTERNS)


# ----------------------------------------------------------------------------
# 헬퍼: ydl_opts 에 cookies/extractor_args 주입
# ----------------------------------------------------------------------------
def apply_cookies(opts: dict, browser: Optional[str]) -> dict:
    """
    주어진 ydl_opts dict 를 *복사* 해서 쿠키/extractor_args 를 주입.

    - browser 가 주어지면: `cookiesfrombrowser` 만 주입. player_client 는
      yt-dlp 기본값 사용 → full format 리스트 확보 (H.264 mp4 매칭 가능).
    - browser=None (쿠키 없이 마지막 fallback): player_client 다양화
      (mweb/tv/web) 로 봇 감지 우회 시도. 단 mobile 용 제한된 format 만
      잡힐 수 있어 format selector 가 매칭 실패할 가능성 있음.

    ★ v0.4.7 — yt-dlp master 의 EJS (External JS) opt-in 강제 대응.
    yt-dlp 가 일정 시점부터 "deno PATH 만으론 부족, EJS 솔버 스크립트 다운로드
    명시적 허용 필요" 로 변경. `--remote-components ejs:github` 옵션 자동 부여.
    옛 yt-dlp 는 알 수 없는 옵션 무시. 새 yt-dlp 는 EJS 솔버 자동 다운/캐싱.
    이 옵션 없으면 n-cipher 챌린지 해결 실패 → "Only images available" → format 매칭 X.
    """
    new_opts = dict(opts)

    # EJS 솔버 다운로드 명시적 허용 — 모든 attempt 에 baseline.
    # 이미 누군가 설정했으면 덮어쓰지 않음.
    new_opts.setdefault("remote_components", ["ejs:github"])

    if browser:
        # (browser, profile, keyring, container) 튜플
        new_opts["cookiesfrombrowser"] = (browser, None, None, None)
        # player_client 손대지 않음 — yt-dlp 기본 (web 우선) 사용
    else:
        # 쿠키 없는 마지막 시도 — player_client 다양화로 봇 감지 우회 시도
        existing_args = new_opts.get("extractor_args") or {}
        merged_args = dict(existing_args)
        for k, v in _YOUTUBE_NOCOOKIES_EXTRACTOR_ARGS.items():
            merged_args.setdefault(k, v)
        new_opts["extractor_args"] = merged_args
    return new_opts


# ----------------------------------------------------------------------------
# Picker — 다운로드 세션 동안 작동하는 브라우저 기억
# ----------------------------------------------------------------------------
LogCallback = Callable[[str, str], None]   # (level, msg) → None


class CookieBrowserPicker:
    """
    한 다운로더 인스턴스 안에서 어떤 브라우저가 작동하는지 학습/재사용.

    사용 패턴
    --------
    picker = CookieBrowserPicker(log=self.log)
    info = picker.run(
        url_label=f"[{idx}/{total}]",
        action=lambda opts: _do_extract_info_or_download(opts, source_url),
        base_opts=base_opts,
    )

    Parameters
    ----------
    log : (level, msg) 콜백. 없으면 silent.
    chain : 시도할 브라우저 리스트. 기본 ``BROWSER_CHAIN``.
    """

    def __init__(
        self,
        log: Optional[LogCallback] = None,
        chain: Iterable[str] = BROWSER_CHAIN,
    ):
        self._log: LogCallback = log or (lambda level, msg: None)
        self._chain: List[str] = list(chain)
        # 이번 세션에서 성공한 브라우저 (다음 호출 때 맨 앞에 둠)
        self._preferred: Optional[str] = None
        # 이번 세션에서 사용 불가로 확인된 브라우저 (다음 호출 때 시도하지 않음)
        self._unavailable: set[str] = set()

    @property
    def preferred(self) -> Optional[str]:
        return self._preferred

    def _ordered_browsers(self) -> List[Optional[str]]:
        """시도 순서: preferred 우선 → 나머지 chain → 마지막 None(쿠키 없이)."""
        order: List[Optional[str]] = []
        if self._preferred and self._preferred not in self._unavailable:
            order.append(self._preferred)
        for b in self._chain:
            if b in self._unavailable:
                continue
            if b not in order:
                order.append(b)
        # 끝에 None — 모든 브라우저 실패 시 쿠키 없이 마지막 시도
        order.append(None)
        return order

    def run(
        self,
        *,
        url_label: str,
        action: Callable[[dict], object],
        base_opts: dict,
    ) -> object:
        """
        ``action(opts)`` 를 브라우저 fallback 체인으로 호출.

        - 성공한 브라우저는 ``self._preferred`` 에 저장돼 다음 ``run()`` 때 우선 사용.
        - 사용 불가 브라우저는 ``self._unavailable`` 에 저장돼 다음 ``run()`` 때 스킵.
        - is_recoverable_error 가 False 이면 즉시 raise (URL 무효 등 진짜 실패).

        Returns
        -------
        action(opts) 의 반환값.

        Raises
        ------
        마지막 시도까지 실패하면 마지막 예외를 그대로 raise.
        """
        last_err: Optional[BaseException] = None

        for browser in self._ordered_browsers():
            opts = apply_cookies(base_opts, browser)
            label = BROWSER_LABELS.get(browser, browser) if browser else "쿠키 없음"
            try:
                result = action(opts)
            except DownloadError as e:
                last_err = e
                err_str = str(e)
                if is_recoverable_error(err_str):
                    if browser:
                        self._log(
                            "warn",
                            f"{url_label} {label} 쿠키로 실패 — 다음 브라우저 시도",
                        )
                    else:
                        # None 까지 왔는데 또 fallback 가능 에러 → 마지막이라 break
                        self._log(
                            "warn",
                            f"{url_label} 쿠키 없이도 실패 — 더 시도할 옵션 없음",
                        )
                    continue
                # 봇/쿠키 무관 에러 → 더 시도해 봤자 동일하게 실패. 즉시 raise.
                raise
            except Exception as e:
                # cookiesfrombrowser 가 OS 에러, FileNotFoundError 등을
                # 그대로 흘리는 경우 (DownloadError 로 wrap 안 되는 케이스).
                last_err = e
                if browser:
                    # 해당 브라우저는 이번 세션 동안 시도하지 않음
                    self._unavailable.add(browser)
                    self._log(
                        "warn",
                        f"{url_label} {label} 사용 불가 ({type(e).__name__}) — 다음 브라우저 시도",
                    )
                    continue
                # 쿠키 없이도 비-DownloadError 면 코드 버그 가능성 → raise
                raise
            else:
                # 성공
                if browser:
                    if self._preferred != browser:
                        self._preferred = browser
                        self._log("ok", f"{url_label} {label} 쿠키로 진행")
                return result

        # for-else 안 쓰고 명시적으로 처리 — 위 루프에서 break 없이 다 실패
        if last_err is not None:
            raise last_err
        # 이론상 도달 불가 — chain 이 비어 있고 None fallback 도 없을 때만
        raise DownloadError("브라우저 fallback 체인이 비어 있습니다.")
