# -*- coding: utf-8 -*-
"""
backend/download.py — yt-dlp wrapper.

URL → 영상 mp4 + source_meta.json 다운로드.
플레이리스트 처리. 진행률 콜백.

설계 원칙:
- H.264/AAC 통합 mp4 우선 (CLAUDE.md QuickTime 호환 정책)
- 영상 1편 실패해도 큐 계속 (caller 가 결정)
- source_meta.json 은 분석·편집이 읽어가는 계약 — schema 따라 정확히 작성
- yt_dlp 는 lazy import (대용량 모듈, 모킹 테스트 시 불필요)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from .filename import build_filename
from .schema import SourceMeta, save_dataclass
from .ytdlp_cookies import CookieBrowserPicker


# CLAUDE.md QuickTime 호환 — H.264/AAC 통합 mp4 우선.
# ★ showdon-downloader v0.4.x 와 동일한 5단 fallback. AVC1/M4A → mp4 → 통합mp4 →
# 임의(VP9/AV1 가능) → best. macOS QuickTime 은 mp4 안 VP9/AV1 디코딩 못 함 →
# 4번 fallback 까지 떨어지면 음성만 나올 수 있음 (VLC 권장). 다운로드 실패보단
# 받아지는 게 우선.
_FORMAT_PREFERENCE = (
    "bv*[vcodec*=avc]+ba[ext=m4a]/"
    "bv*[ext=mp4]+ba[ext=m4a]/"
    "b[ext=mp4]/"
    "bv*+ba/"
    "best"
)
# ★ mp4 단일 → mp4/webm/mkv 다중 허용. YouTube 가 일부 영상 (특히 짧은 해외 쇼츠)
# 은 VP9+Opus (webm) 만 제공 → mp4 만 허용하면 yt-dlp 가 호환 체크에서 거부 →
# "Requested format is not available". webm/mkv 허용으로 회피.
_MERGE_OUTPUT_FORMAT = "mp4/webm/mkv"

# yt-dlp 가 _speed_str/_eta_str 에 박는 ANSI 컬러 escape 제거용
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_ESCAPE.sub("", s).strip() if s else ""


# yt-dlp extractor key → 우리 platform 라벨 매핑
_PLATFORM_MAP = {
    "youtube": "youtube",
    "youtubeshorts": "youtube",
    "tiktok": "tiktok",
    "instagram": "instagram",
    "instagramreels": "instagram",
    "threads": "threads",
    "x": "x",
    "twitter": "x",
}


# =============================================================================
# 진행 이벤트
# =============================================================================

DownloadStatus = Literal[
    "fetching_meta",   # yt-dlp 메타 추출 중
    "downloading",     # 다운로드 진행 중
    "postprocessing",  # 영상+오디오 머지 중
    "completed",       # 완료
    "failed",          # 실패
    "skipped",         # 이미 있어서 스킵
    "cancelled",       # 사용자 취소
]


@dataclass
class DownloadProgress:
    """다운로드 진행 이벤트. GUI 콜백 인자."""
    status: DownloadStatus
    url: str = ""
    title: str = ""
    folder: Optional[Path] = None
    percent: float = 0.0          # 0.0 ~ 100.0
    speed_str: str = ""
    eta_str: str = ""
    message: str = ""
    error: Optional[str] = None


ProgressCallback = Callable[[DownloadProgress], None]
CancelCheck = Callable[[], bool]


class DownloadError(RuntimeError):
    """다운로드 실패 (네트워크·플랫폼·권한 등)."""


class DownloadCancelled(Exception):
    """사용자 취소 — yt-dlp progress hook 안에서 raise."""


# =============================================================================
# 메타데이터 추출 (다운로드 없이)
# =============================================================================

def fetch_metadata(
    url: str,
    *,
    extract_flat: bool = False,
    picker: Optional["CookieBrowserPicker"] = None,
) -> dict:
    """yt-dlp 로 영상 메타만 추출. 다운로드 X.

    Args:
        url: 영상 URL (단일 또는 플레이리스트)
        extract_flat: True 면 플레이리스트 entries 의 풀 메타 빼고 entry id 만.
            플레이리스트 펼치기 빠르게.
        picker: 호출 측에서 공유하는 CookieBrowserPicker. 없으면 새로 생성.
            여러 호출에 같은 picker 를 넘기면 성공한 브라우저가 캐시되어 빠름.

    Returns:
        yt-dlp info dict. 플레이리스트면 _type='playlist' + 'entries' 키 포함.

    ★ YouTube 봇 감지 + EJS opt-in + cookies fallback 자동 처리 (showdon-downloader
    v0.4.0~0.4.7 와 동일 패턴).
    """
    import yt_dlp  # lazy
    base_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "extract_flat": extract_flat,
    }
    if picker is None:
        picker = CookieBrowserPicker(log=lambda lv, msg: None)

    def _do_extract(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return picker.run(
        url_label=f"[메타:{url[:60]}]",
        action=_do_extract,
        base_opts=base_opts,
    )


def is_playlist(info: dict) -> bool:
    return info.get("_type") == "playlist" or "entries" in info


def expand_playlist(
    url: str,
    *,
    picker: Optional["CookieBrowserPicker"] = None,
) -> list[dict]:
    """플레이리스트 URL 또는 단일 URL → 영상 entry dict 리스트.

    각 entry 에 webpage_url 보장 (url 필드로 fallback).
    extract_flat 모드 — 빠르고 가벼움. 풀 메타는 다운로드 시점에 다시 가져옴.
    """
    info = fetch_metadata(url, extract_flat=True, picker=picker)
    if not is_playlist(info):
        return [info]
    entries = info.get("entries") or []
    out: list[dict] = []
    for e in entries:
        if e is None:
            continue
        if not e.get("webpage_url") and e.get("url"):
            e = {**e, "webpage_url": e["url"]}
        out.append(e)
    return out


# =============================================================================
# source_meta.json 생성
# =============================================================================

def _normalize_platform(raw: str | None) -> str:
    """yt-dlp extractor 이름 → 우리 platform 문자열."""
    if not raw:
        return "unknown"
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    return _PLATFORM_MAP.get(key, key or "unknown")


def _format_iso_date(yyyymmdd: str | None) -> Optional[str]:
    """yt-dlp 의 'YYYYMMDD' → ISO 'YYYY-MM-DD'."""
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _build_source_meta(info: dict) -> SourceMeta:
    """yt-dlp info dict → SourceMeta dataclass."""
    uploader_id = info.get("uploader_id") or info.get("channel_id") or ""
    handle: Optional[str] = None
    if uploader_id:
        handle = uploader_id if uploader_id.startswith("@") else f"@{uploader_id}"

    return SourceMeta(
        url=info.get("webpage_url") or info.get("original_url") or "",
        platform=_normalize_platform(info.get("extractor_key") or info.get("extractor")),
        uploader=info.get("uploader") or info.get("channel") or "",
        uploader_handle=handle,
        channel_url=info.get("channel_url"),
        title=info.get("title") or "",
        upload_date=_format_iso_date(info.get("upload_date")),
        duration_s=float(info.get("duration") or 0.0),
        downloaded_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


# =============================================================================
# 다운로드
# =============================================================================

def _make_progress_hook(
    callback: ProgressCallback | None,
    is_cancelled: CancelCheck | None,
    url: str,
    title: str,
) -> Callable[[dict], None]:
    """yt-dlp progress_hooks 호환 함수 생성.

    is_cancelled 가 True 를 반환하면 DownloadCancelled 를 raise — yt-dlp 가 다운로드 중단.
    """
    def hook(d: dict) -> None:
        if is_cancelled and is_cancelled():
            raise DownloadCancelled()
        if callback is None:
            return
        status = d.get("status", "")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100.0) if total else 0.0
            callback(DownloadProgress(
                status="downloading",
                url=url,
                title=title,
                percent=pct,
                speed_str=_strip_ansi(d.get("_speed_str", "")),
                eta_str=_strip_ansi(d.get("_eta_str", "")),
            ))
        elif status == "finished":
            callback(DownloadProgress(
                status="postprocessing",
                url=url,
                title=title,
                percent=100.0,
                message="머지 중",
            ))
        elif status == "error":
            callback(DownloadProgress(
                status="failed",
                url=url,
                title=title,
                error=str(d.get("error") or "yt-dlp error"),
            ))

    return hook


def download_video(
    url: str,
    data_root: Path | str,
    *,
    on_progress: ProgressCallback | None = None,
    is_cancelled: CancelCheck | None = None,
    overwrite: bool = False,
) -> Path:
    """단일 영상 다운로드. 영상별 폴더 구조로 저장.

    Args:
        url: 영상 URL
        data_root: ~/showdon/yejjas (모든 영상 부모 폴더의 루트)
        on_progress: 진행률 콜백 (선택)
        is_cancelled: 취소 체크 콜백. True 반환 시 DownloadCancelled raise.
        overwrite: True 면 같은 영상 있어도 덮어쓰기. False 면 skip.

    Returns:
        Path: 영상 부모 폴더 (~/showdon/yejjas/<영상명>/)
            안에 원본/video.mp4 + 원본/source_meta.json
            추후 분석/, 편집점/, 완성/ 도 같은 부모 아래 생성됨

    Raises:
        DownloadError: 메타 추출 또는 다운로드 실패
        DownloadCancelled: 사용자 취소
    """
    import yt_dlp  # lazy

    data_root = Path(data_root).expanduser()
    data_root.mkdir(parents=True, exist_ok=True)

    if is_cancelled and is_cancelled():
        raise DownloadCancelled()

    # 1. 메타 추출 (다운로드 없이) — 영상명 폴더 결정
    if on_progress:
        on_progress(DownloadProgress(
            status="fetching_meta", url=url, message="메타 추출 중"
        ))
    # picker — 이 영상 처리 동안 공유 (메타 + 다운로드 동일 브라우저 재사용).
    # showdon-downloader 와 다르게 yejja 는 module-level function 이라 인스턴스 X.
    # download_video 호출마다 새 picker 생성 (overhead 미미).
    _picker = CookieBrowserPicker(log=lambda lv, msg: None)
    try:
        info = fetch_metadata(url, picker=_picker)
    except Exception as e:
        raise DownloadError(f"메타 추출 실패: {e}") from e

    if is_playlist(info):
        raise DownloadError(
            "플레이리스트 URL 입니다. download_videos() 또는 expand_playlist() 사용하세요."
        )

    title = info.get("title") or ""
    filename = build_filename(
        upload_date=info.get("upload_date"),
        platform=_normalize_platform(info.get("extractor_key") or info.get("extractor")),
        title=title,
        ext="mp4",
    )
    basename = filename.removesuffix(".mp4")

    # 영상 부모 폴더 = data_root/<basename>/
    # 그 안에 원본/, 분석/, 편집점/, 완성/ 가 생성됨
    video_root = data_root / basename
    originals = video_root / "원본"

    # 2. 이미 있으면 스킵 (또는 덮어쓰기)
    # 영상 파일명 = 부모 폴더명과 동일 → Finder 에서 파일만 봐도 식별 가능
    video_path = originals / f"{basename}.mp4"
    if video_path.exists() and not overwrite:
        if on_progress:
            on_progress(DownloadProgress(
                status="skipped", url=url, title=title, folder=video_root,
                message="이미 존재 — 스킵",
            ))
        return video_root

    originals.mkdir(parents=True, exist_ok=True)

    # 3. 다운로드 — 원본/<basename>.mp4
    # outtmpl 의 basename 이 파일 시스템 안전한 문자라 그대로 사용 (filename.py 가 정제)
    base_opts = {
        "format": _FORMAT_PREFERENCE,
        "outtmpl": str(originals / f"{basename}.%(ext)s"),
        "merge_output_format": _MERGE_OUTPUT_FORMAT,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "progress_hooks": [_make_progress_hook(on_progress, is_cancelled, url, title)],
        "overwrites": overwrite,
        # ★ v1.9.5 — 자막 다운로드 (analyze.py 의 _detect_and_copy_transcripts 가 활용).
        # manual 우선, 없으면 auto fallback. ko (dialog ground truth) + en (해외 reference).
        # info.json = manual/auto 구별 메타 (필수).
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ko", "en"],
        "subtitlesformat": "srt/vtt/best",
        "postprocessors": [{
            "key": "FFmpegSubtitlesConvertor",
            "format": "srt",
        }],
        "writeinfojson": True,
    }
    # ★ showdon-downloader v0.4.0~0.4.7 와 동일 — picker 경유로 cookies fallback +
    # EJS opt-in + tv_simply 자동 적용. _picker 는 메타 추출에서 이미 성공 브라우저
    # 캐시 상태라 다운로드는 같은 브라우저로 단번에 진행.
    # DownloadCancelled (progress hook 에서 raise) 는 picker 가 generic Exception 으로
    # 잡아서 fallback 시도하지 않도록, DownloadError sentinel 로 변환 → picker 가
    # non-recoverable 로 즉시 raise → 외부에서 sentinel 매칭으로 DownloadCancelled 복원.
    _CANCEL_SENTINEL = "__YEJJA_USER_CANCELLED__"

    def _do_download(opts):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except DownloadCancelled:
            raise DownloadError(_CANCEL_SENTINEL) from None

    try:
        full_info = _picker.run(
            url_label=f"[다운로드:{title[:40]}]",
            action=_do_download,
            base_opts=base_opts,
        )
    except DownloadError as e:
        if _CANCEL_SENTINEL in str(e):
            if on_progress:
                on_progress(DownloadProgress(status="cancelled", url=url, title=title))
            raise DownloadCancelled() from None
        raise DownloadError(f"다운로드 실패: {e}") from e
    except DownloadCancelled:
        if on_progress:
            on_progress(DownloadProgress(status="cancelled", url=url, title=title))
        raise
    except Exception as e:
        raise DownloadError(f"다운로드 실패: {e}") from e

    # 4. source_meta.json 저장 (원본/ 안)
    source_meta = _build_source_meta(full_info)
    save_dataclass(source_meta, originals / "source_meta.json")

    if on_progress:
        on_progress(DownloadProgress(
            status="completed", url=url, title=title, folder=video_root,
            percent=100.0, message="완료",
        ))

    return video_root


def download_videos(
    urls_or_playlist: str | list[str],
    data_root: Path | str,
    *,
    on_progress: ProgressCallback | None = None,
    is_cancelled: CancelCheck | None = None,
    overwrite: bool = False,
    on_video_done: Callable[[Path], None] | None = None,
) -> list[Path]:
    """여러 영상 (또는 플레이리스트) 다운로드.

    한 영상 실패해도 다음 영상 계속 진행. 실패는 progress 콜백 'failed' 로만 알림.
    is_cancelled 가 True 면 큐 중단 (현재 영상 끝나면 멈춤).

    Args:
        urls_or_playlist: URL 1개, URL 리스트, 또는 플레이리스트 URL 1개
        data_root: data_root (~/showdon/yejjas) — 영상별 폴더가 만들어지는 부모
        on_progress: 영상별 진행률 이벤트
        is_cancelled: 영상 사이 + 다운로드 중 취소 체크
        overwrite: True 면 기존 영상 덮어쓰기
        on_video_done: 영상 1편 완료 시 호출 (영상 부모 폴더 path 전달 — 분석 큐 자동 등록용)

    Returns:
        성공한 영상의 부모 폴더 경로 리스트
    """
    if isinstance(urls_or_playlist, str):
        urls = [urls_or_playlist]
    else:
        urls = [u.strip() for u in urls_or_playlist if u and u.strip()]

    # 플레이리스트 펼치기
    expanded: list[str] = []
    for url in urls:
        try:
            entries = expand_playlist(url)
        except Exception as e:
            if on_progress:
                on_progress(DownloadProgress(status="failed", url=url, error=str(e)))
            continue
        for e in entries:
            wu = e.get("webpage_url") or e.get("url")
            if wu:
                expanded.append(wu)

    succeeded: list[Path] = []
    for u in expanded:
        if is_cancelled and is_cancelled():
            break
        try:
            video_root = download_video(
                u, data_root,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
                overwrite=overwrite,
            )
            succeeded.append(video_root)
            if on_video_done:
                on_video_done(video_root)
        except DownloadCancelled:
            break
        except DownloadError as e:
            if on_progress:
                on_progress(DownloadProgress(status="failed", url=u, error=str(e)))
            continue

    return succeeded


# =============================================================================
# 스모크 테스트 — `python -m backend.download` 또는 인자로 URL 던지기
# =============================================================================

if __name__ == "__main__":
    import argparse
    import sys

    # 패키지 모드 아닌 직접 실행 시 sys.path 보정
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    parser = argparse.ArgumentParser(description="다운로드 backend 스모크 테스트")
    parser.add_argument("url", nargs="?", help="영상 URL (없으면 모킹 테스트만 실행)")
    parser.add_argument("-o", "--out", default="/tmp/yejja_test", help="저장 폴더")
    parser.add_argument("--meta-only", action="store_true",
                        help="다운로드 없이 메타만 추출")
    args = parser.parse_args()

    # 1. 모킹 테스트 — yt-dlp 없이 _build_source_meta 검증
    print("=== 모킹 SourceMeta 빌드 (네트워크 없이) ===")
    fake_info = {
        "webpage_url": "https://www.youtube.com/watch?v=abc",
        "extractor_key": "Youtube",
        "uploader": "유병재",
        "uploader_id": "yubyungjae",
        "channel_url": "https://www.youtube.com/@yubyungjae",
        "title": "n년째 고용 중인 투컷 친구 대행 알바",
        "upload_date": "20260430",
        "duration": 45.0,
    }
    meta = _build_source_meta(fake_info)
    summary = {
        "url": meta.url,
        "platform": meta.platform,
        "uploader": meta.uploader,
        "handle": meta.uploader_handle,
        "title": meta.title,
        "upload_date": meta.upload_date,
        "duration_s": meta.duration_s,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    assert meta.platform == "youtube"
    assert meta.uploader_handle == "@yubyungjae"
    assert meta.upload_date == "2026-04-30"
    assert meta.uploader == "유병재"
    assert meta.duration_s == 45.0
    print("[OK] _build_source_meta 통과")

    # 2. 플랫폼 정규화
    print("\n=== platform 정규화 ===")
    cases = {
        "Youtube": "youtube", "YoutubeShorts": "youtube",
        "Tiktok": "tiktok", "TikTok": "tiktok",
        "Instagram": "instagram", "InstagramReels": "instagram",
        "Twitter": "x", "X": "x",
        None: "unknown", "": "unknown",
        "SomeRandom": "somerandom",
    }
    for raw, expected in cases.items():
        got = _normalize_platform(raw)
        assert got == expected, f"{raw!r} → {got!r}, expected {expected!r}"
    print(f"[OK] {len(cases)}개 케이스 통과")

    # 3. 날짜 변환
    print("\n=== upload_date 변환 ===")
    assert _format_iso_date("20260430") == "2026-04-30"
    assert _format_iso_date(None) is None
    assert _format_iso_date("invalid") is None
    assert _format_iso_date("202604") is None  # 짧음
    print("[OK] _format_iso_date 통과")

    # 4. 파일명 빌드 (filename.py 통합)
    print("\n=== build_filename 통합 ===")
    fn = build_filename(
        upload_date=fake_info["upload_date"],
        platform=meta.platform,
        title=fake_info["title"],
    )
    expected_fn = "260430-youtube-n년째_고용_중인_투컷_친구_대행_알바.mp4"
    assert fn == expected_fn, f"{fn} != {expected_fn}"
    print(f"[OK] {fn}")

    # 5. 실제 URL 테스트 (인자로 줘야 실행됨)
    if not args.url:
        print("\n=== 모든 모킹 테스트 통과 ===")
        print("\n실제 다운로드/메타 테스트를 하려면:")
        print(f"  python -m backend.download --meta-only 'https://...'")
        print(f"  python -m backend.download 'https://...' -o /tmp/test")
        sys.exit(0)

    def progress(p: DownloadProgress) -> None:
        if p.status == "downloading":
            print(f"  [{p.percent:5.1f}%] {p.speed_str} ETA {p.eta_str}", flush=True)
        elif p.status in ("fetching_meta", "postprocessing", "completed",
                          "skipped", "cancelled"):
            print(f"  [{p.status}] {p.message}")
        elif p.status == "failed":
            print(f"  [FAIL] {p.error}")

    if args.meta_only:
        print(f"\n=== 메타 추출: {args.url} ===")
        try:
            info = fetch_metadata(args.url)
            sm = _build_source_meta(info)
            print(json.dumps({
                "title": sm.title,
                "uploader": sm.uploader,
                "handle": sm.uploader_handle,
                "platform": sm.platform,
                "upload_date": sm.upload_date,
                "duration_s": sm.duration_s,
                "url": sm.url,
            }, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"\n=== 다운로드: {args.url} ===")
        try:
            folder = download_video(args.url, args.out, on_progress=progress)
            print(f"\n[OK] 다운로드 완료: {folder}")
            for f in sorted(folder.iterdir()):
                size_mb = f.stat().st_size / 1024 / 1024
                print(f"  - {f.name}  ({size_mb:.2f} MB)")
        except DownloadError as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            sys.exit(1)
        except DownloadCancelled:
            print("[취소됨]")
            sys.exit(130)
