# -*- coding: utf-8 -*-
"""
backend/analyze.py — 1단계 영상 분석 코어.

원본 영상 1편을 받아 PRD §3 의 분석 산출물을 생성:
  - meta.json          (AnalysisMeta)
  - stt.json           (SttJson)
  - scene_cuts.json    (SceneCutsJson)
  - ocr_candidates.json (OcrCandidatesJson)
  - face_clusters.json (FaceClustersJson)
  - frames/f_NNNNN.jpg  (1초당 1프레임, 720x-2 다운스케일)

단계별 실패는 _PARTIAL 마커, 전체 실패는 _FAILED, 모두 성공은 _DONE.
heavy import (faster_whisper, cv2, mediapipe) 는 lazy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .schema import (
    MARKER_DONE, MARKER_FAILED, MARKER_PARTIAL,
    AnalysisError, AnalysisMeta, AnalysisStatus,
    AppConfig, AudioInfo, CandidateKind,
    CastListCandidate, CastListCandidatesJson,
    FaceBboxNorm, FaceCluster, FaceClustersJson, FaceDetection, FrameFaces,
    OcrCandidate, OcrCandidatesJson, OcrLocalEntry, OcrLocalJson, OcrTextRegion,
    SceneCutsJson, SourceMeta,
    StepStatus, SttJson, SttSegment, SttWord,
    VideoInfo,
    clear_status_markers, load_dataclass, save_dataclass, write_marker,
)
from .edit import _resolve_bin  # ffmpeg/ffprobe 절대경로 helper (.app PATH 누락 대응)


# =============================================================================
# 진행 이벤트
# =============================================================================

@dataclass
class AnalysisProgressEvent:
    """단계 진행 이벤트. GUI 가 받아 진행률 바 갱신."""
    step: str                       # ffprobe | stt | scene_detection | frame_extraction | ocr_candidates | face_clusters
    status: StepStatus
    percent: float = 0.0            # 단계 내 0~100, 모르면 0
    elapsed_s: float = 0.0
    message: str = ""


ProgressCb = Callable[[AnalysisProgressEvent], None]
CancelCb = Callable[[], bool]


class AnalysisCancelled(Exception):
    """사용자 취소 — orchestrator 가 받아서 정리."""


class AnalysisError_(RuntimeError):
    """분석 단계 오류 (네트워크·모델·파일 등)."""


# =============================================================================
# 단계 1: ffprobe — 영상 메타
# =============================================================================

def _ffprobe(video_path: Path) -> tuple[VideoInfo, AudioInfo, float, int]:
    """ffprobe 로 메타 추출. (video_info, audio_info, duration_s, size_bytes) 반환."""
    cmd = [
        _resolve_bin("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AnalysisError_(f"ffprobe 실패: {r.stderr.strip()}")

    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    fmt = info.get("format", {})

    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    astream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    fps_str = vstream.get("r_frame_rate", "0/0")
    try:
        a, b = fps_str.split("/")
        fps = round(float(a) / float(b), 2) if float(b) else 0.0
    except Exception:
        fps = 0.0

    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    aspect = ""
    if width and height:
        # 약수 줄여 16:9 같은 표기
        from math import gcd
        g = gcd(width, height)
        aspect = f"{width // g}:{height // g}"

    video_info = VideoInfo(
        width=width,
        height=height,
        fps=fps,
        codec=vstream.get("codec_name", ""),
        bit_rate=int(vstream.get("bit_rate") or fmt.get("bit_rate") or 0),
        aspect_ratio=aspect,
    )
    audio_info = AudioInfo(
        present=bool(astream),
        codec=astream.get("codec_name") if astream else None,
        sample_rate=int(astream.get("sample_rate", 0)) if astream else None,
        channels=int(astream.get("channels", 0)) if astream else None,
    )
    duration_s = float(fmt.get("duration") or 0.0)
    size_bytes = int(fmt.get("size") or 0)
    return video_info, audio_info, duration_s, size_bytes


# =============================================================================
# 단계 1b: 자막 감지 + transcript_source.json (★ v1.9.5 NEW)
# yt-dlp 가 영상과 같이 받은 manual/auto 자막을 분석 폴더 subs/ 로 복사.
# info.json 의 subtitles (manual) / automatic_captions (auto) 키로 정확히 구별.
# 코워크가 dialog 작성 시 우선순위 결정 (manual > stt > auto).
# =============================================================================

def _detect_and_copy_transcripts(
    video_path: Path,
    output_dir: Path,
) -> "TranscriptSourceJson":
    """video_path 와 같은 디렉토리에서 yt-dlp 자막 + info.json 감지.

    감지 대상:
      - <stem>.ko.srt / <stem>.en.srt — yt-dlp `--write-subs` 결과
      - <stem>.info.json — yt-dlp `--writeinfojson` 결과 (manual/auto 구별 메타)

    info.json 'subtitles' 키 = manual 자막, 'automatic_captions' = auto 자막.
    yt-dlp 는 manual 우선 다운 (둘 다 옵션 켜놔도). 같은 파일명 (.ko.srt) 로 받기 때문에
    info.json 메타 없이는 manual/auto 구별 불가 → info.json 의무.

    info.json 없으면 manual 가정 (기존 영상 / 직접 박은 srt 케이스).
    자막 파일은 output_dir/subs/<lang>.srt 로 복사 (분석 결과 폴더 이주 시 손실 방지).
    """
    # lazy import (TranscriptSourceJson 의 schema_version 등 의존)
    from backend.schema import TranscriptSourceJson, TranscriptSourceLang

    parent = video_path.parent
    stem = video_path.stem
    info_path = parent / f"{stem}.info.json"
    subs_dir = output_dir / "subs"

    result = TranscriptSourceJson()

    # info.json 로딩 (있으면 manual/auto 정확히 구별 / 없으면 manual 가정)
    info: dict = {}
    if info_path.exists():
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            try:
                result.info_json_relpath = str(info_path.name)
            except Exception:
                result.info_json_relpath = info_path.name
        except Exception:
            info = {}

    manual_subs = (info.get("subtitles") or {}) if isinstance(info, dict) else {}
    auto_subs = (info.get("automatic_captions") or {}) if isinstance(info, dict) else {}

    for lang in ("ko", "en"):
        srt_path = parent / f"{stem}.{lang}.srt"
        if not srt_path.exists():
            continue

        # type 결정 (info.json 있으면 정확 / 없으면 manual 가정)
        if info:
            if lang in manual_subs:
                sub_type = "manual"
            elif lang in auto_subs:
                sub_type = "auto"
            else:
                # info 에 메타 없는데 srt 파일 있으면 사용자가 직접 박은 것 — manual 가정
                sub_type = "manual"
        else:
            sub_type = "manual"

        # subs/ 로 복사
        try:
            subs_dir.mkdir(parents=True, exist_ok=True)
            dest = subs_dir / f"{lang}.srt"
            import shutil
            shutil.copy2(srt_path, dest)
        except Exception:
            continue

        # segment count (디버그용 — srt 의 number prefix `^\d+$` 카운트)
        seg_count = 0
        try:
            with open(dest, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().isdigit():
                        seg_count += 1
        except Exception:
            pass

        result.sources.append(TranscriptSourceLang(
            lang=lang,
            type=sub_type,
            path=f"subs/{lang}.srt",
            segment_count=seg_count,
        ))

    # preference 결정 — manual 한 lang 이라도 있으면 manual / 없으면 auto / 없으면 none
    has_manual = any(s.type == "manual" for s in result.sources)
    has_auto = any(s.type == "auto" for s in result.sources)
    if has_manual:
        result.preference = "manual"
    elif has_auto:
        result.preference = "auto"
    else:
        result.preference = "none"

    return result


# =============================================================================
# 단계 2: STT — mlx-whisper (Apple MLX / Metal GPU)
# ★ v1.9.3+ — faster-whisper 제거됨 (검증 결과 mlx 6.6x 빠름, 정확도 동일)
# =============================================================================

def _stt_mlx(
    video_path: Path,
    *,
    mlx_model_repo: str,
    language: Optional[str],
    duration_s: float,
    initial_prompt: str = "",
    on_progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
    temperature: float = 0.0,
    condition_on_previous: bool = False,
) -> SttJson:
    """mlx-whisper 로 영상 전체 STT. Apple Silicon Metal GPU 활용.

    faster-whisper 대비 5~10x 빠름. 결과 schema 는 SttJson 으로 통일.

    mlx-whisper 의 transcribe() 결과 dict:
      {
        'text': str,
        'language': str,
        'segments': [
          {'id', 'start', 'end', 'text', 'avg_logprob', 'no_speech_prob',
           'compression_ratio', 'words': [{'start', 'end', 'word', 'probability'}]}
        ]
      }

    mlx-whisper 는 vad_filter / cpu_threads 미지원 (GPU 기반).
    """
    import mlx_whisper  # lazy

    if on_progress:
        on_progress(AnalysisProgressEvent(
            step="stt", status=StepStatus.RUNNING, percent=0.0,
            message=f"mlx-whisper 모델 로드 ({mlx_model_repo})",
        ))

    result = mlx_whisper.transcribe(
        str(video_path),
        path_or_hf_repo=mlx_model_repo,
        language=language,
        word_timestamps=True,
        initial_prompt=initial_prompt or None,
        temperature=temperature,
        condition_on_previous_text=condition_on_previous,
    )

    raw_segments = result.get("segments", [])
    segments: list[SttSegment] = []

    for s in raw_segments:
        if is_cancelled and is_cancelled():
            raise AnalysisCancelled()

        seg = SttSegment(
            id=len(segments) + 1,
            start=round(float(s.get("start", 0)), 3),
            end=round(float(s.get("end", 0)), 3),
            text=(s.get("text") or "").strip(),
            no_speech_probability=round(float(s.get("no_speech_prob", 0.0)), 3),
            avg_logprob=round(float(s.get("avg_logprob", 0.0)), 3),
            compression_ratio=round(float(s.get("compression_ratio", 0.0)), 3),
            words=[
                SttWord(
                    start=round(float(w.get("start", 0)), 3),
                    end=round(float(w.get("end", 0)), 3),
                    word=w.get("word", ""),
                    probability=round(float(w.get("probability", 0.0)), 3),
                )
                for w in (s.get("words") or [])
            ],
        )
        segments.append(seg)

        if on_progress and duration_s > 0:
            pct = min(100.0, seg.end / duration_s * 100.0)
            on_progress(AnalysisProgressEvent(
                step="stt", status=StepStatus.RUNNING,
                percent=pct,
                message=f"{len(segments)} 세그먼트 ({seg.end:.1f}/{duration_s:.0f}s)",
            ))

    return SttJson(
        model=f"mlx-whisper-{mlx_model_repo.split('/')[-1]}",
        compute_type="mlx-fp16",
        language=result.get("language", language or "ko"),
        language_probability=1.0,  # mlx-whisper 는 prob 미반환
        duration_s=round(duration_s, 3),
        segment_count=len(segments),
        vad_filter=False,  # mlx-whisper 는 vad_filter 미지원
        segments=segments,
    )


# =============================================================================
# 단계 3: scene detection — ffmpeg
# =============================================================================

def _scenes(
    video_path: Path,
    *,
    threshold: float,
    duration_s: float,
    is_cancelled: CancelCb | None = None,
) -> SceneCutsJson:
    """ffmpeg scene detection.

    `select='gt(scene,N)',showinfo` → stderr 의 pts_time 파싱.
    실시간 진행률은 v1.0 미지원 (capture_output 으로 단순 처리). v1.1 개선 예정.
    """
    if is_cancelled and is_cancelled():
        raise AnalysisCancelled()

    cmd = [
        _resolve_bin("ffmpeg"), "-i", str(video_path),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
        "-loglevel", "info",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    # ffmpeg 는 정상 처리 후에도 stderr 에 출력. returncode 0 또는 1 둘 다 허용.
    if r.returncode not in (0, 1):
        raise AnalysisError_(f"ffmpeg scene detection 실패 (rc={r.returncode}): {r.stderr[:300]}")

    times = re.findall(r"pts_time:([\d\.]+)", r.stderr)
    cut_times = sorted({round(float(t), 3) for t in times})

    return SceneCutsJson(
        threshold=threshold,
        cut_count=len(cut_times),
        cut_times=cut_times,
    )


# =============================================================================
# 단계 4: 프레임 추출 — ffmpeg
# =============================================================================

def _extract_frames(
    video_path: Path,
    frames_dir: Path,
    *,
    fps_target: float = 1.0,
    scale_w: int = 720,
    is_cancelled: CancelCb | None = None,
) -> int:
    """fps_target 초당 1프레임 (또는 N) + 가로 scale_w 다운스케일 (세로 비율 유지).

    이미 frames/f_*.jpg 있으면 재사용 (재실행 시 빠름).
    """
    if is_cancelled and is_cancelled():
        raise AnalysisCancelled()

    frames_dir.mkdir(parents=True, exist_ok=True)
    existing = list(frames_dir.glob("f_*.jpg"))
    if existing:
        return len(existing)

    # `-vf fps=N,scale=W:-2` — 가로 W, 세로 비율 유지 (짝수)
    cmd = [
        _resolve_bin("ffmpeg"), "-y", "-i", str(video_path),
        "-vf", f"fps={fps_target},scale={scale_w}:-2",
        "-q:v", "3",
        str(frames_dir / "f_%05d.jpg"),
        "-loglevel", "error",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AnalysisError_(f"ffmpeg 프레임 추출 실패: {r.stderr[:300]}")

    return len(list(frames_dir.glob("f_*.jpg")))


# =============================================================================
# 단계 5: ocr_candidates — STT + gap + scene 통합
# =============================================================================

def _frame_path_for_time(t_abs: float, frame_count: int, fps_target: float) -> str:
    """t_abs (초) → 'frames/f_NNNNN.jpg' (1-indexed).

    fps=1.0 가정 시 frame N (1-indexed) 은 t∈[N-1, N) 구간 대표.
    """
    idx = int(t_abs * fps_target) + 1
    idx = max(1, min(idx, max(frame_count, 1)))
    return f"frames/f_{idx:05d}.jpg"


def _build_ocr_candidates(
    stt: Optional[SttJson],
    scenes: Optional[SceneCutsJson],
    frame_count: int,
    fps_target: float,
    *,
    gap_min_s: float = 1.0,
    mid_offset_s: float = 0.5,
    near_dedup_s: float = 0.3,
) -> OcrCandidatesJson:
    """STT 발화 시점 + 발화 빈 구간 + scene 시점 통합한 OCR 후보 시점 리스트.

    PRD §3.4 따름. Claude Vision 이 각 후보의 frame.jpg 보고 OCR.
    """
    # (t_abs, kind, stt_segment_id, stt_text_hint) tuple 로 모음
    points: list[tuple[float, CandidateKind, Optional[int], str]] = []

    # 1. STT 시점 (발화 시작 + mid_offset 후)
    segs: list[SttSegment] = []
    if stt and stt.segments:
        segs = sorted(stt.segments, key=lambda s: s.start)
        for s in segs:
            t = s.start + mid_offset_s
            if t > s.end:
                t = (s.start + s.end) / 2.0
            points.append((t, CandidateKind.STT, s.id, s.text))

        # 2. Gap 시점 (1초 이상 발화 빈 구간)
        if gap_min_s > 0:
            # 사이 gap
            for i in range(len(segs) - 1):
                gs, ge = segs[i].end, segs[i + 1].start
                if ge - gs >= gap_min_s:
                    t = gs + mid_offset_s
                    if t < ge:
                        points.append((t, CandidateKind.GAP, None, ""))
            # 첫 발화 전
            if segs and segs[0].start >= gap_min_s:
                points.append((mid_offset_s, CandidateKind.GAP, None, ""))

    # 3. Scene 시점 (이미 stt/gap 근처에 있으면 dedup)
    existing_times = sorted(p[0] for p in points)

    def _near_existing(t: float) -> bool:
        # 이진 탐색이면 더 빠르지만 N 작아 무시
        return any(abs(t - et) < near_dedup_s for et in existing_times)

    if scenes and scenes.cut_times:
        for st in scenes.cut_times:
            if _near_existing(st):
                continue
            points.append((st, CandidateKind.SCENE, None, ""))

    # 시간 순 정렬 + 인덱스 부여 + 프레임 매칭
    points.sort(key=lambda p: p[0])
    candidates: list[OcrCandidate] = []
    for i, (t, kind, sid, text) in enumerate(points, 1):
        candidates.append(OcrCandidate(
            i=i,
            t_abs=round(t, 3),
            kind=kind,
            frame=_frame_path_for_time(t, frame_count, fps_target),
            stt_segment_id=sid,
            stt_text_hint=text,
        ))

    return OcrCandidatesJson(
        candidate_count=len(candidates),
        fps_target=fps_target,
        candidates=candidates,
    )


# =============================================================================
# 단계 5b: 로컬 OCR 더블체킹 — Apple Vision (default) / PaddleOCR (★ v1.9.3+)
# =============================================================================

def _ocr_local_apple_vision(
    candidates: OcrCandidatesJson,
    analysis_dir: Path,
    *,
    languages: Optional[list[str]] = None,
    min_score: float = 0.5,
    on_progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> OcrLocalJson:
    """★ v1.9.3+ macOS Vision framework 으로 OCR. PaddleOCR 보다 ~10x 빠름.

    requires: macOS 12+ + pyobjc-framework-Vision
    Apple Silicon Metal GPU 활용. 한국어 정확도 우수 (macOS 14+).
    """
    import sys
    if sys.platform != "darwin":
        raise NotImplementedError(
            "Apple Vision OCR 은 macOS 만 지원. local_ocr_engine='paddleocr' 사용"
        )

    # lazy import (다른 OS 에서 import 에러 방지)
    from Foundation import NSURL, NSAutoreleasePool
    from Quartz import CIImage
    import Vision  # pyobjc-framework-Vision

    if languages is None:
        languages = ["ko-KR", "en-US"]

    entries: list[OcrLocalEntry] = []
    total = len(candidates.candidates)
    last_emit_at = 0.0

    for idx, cand in enumerate(candidates.candidates):
        if is_cancelled and is_cancelled():
            raise AnalysisCancelled()

        frame_path = analysis_dir / cand.frame
        if not frame_path.exists():
            continue

        # ★ v1.9.5 — frame 단위 NSAutoreleasePool 으로 ObjC 객체 (NSURL/CIImage/VNRequest/VNHandler)
        # 즉시 release 강제. 긴 영상 (수천 frame) 처리 시 ObjC 메모리 누적 → Qt paint 충돌 회피.
        pool = NSAutoreleasePool.alloc().init()
        try:
            try:
                url = NSURL.fileURLWithPath_(str(frame_path))
                image = CIImage.imageWithContentsOfURL_(url)
                if image is None:
                    continue

                request = Vision.VNRecognizeTextRequest.alloc().init()
                request.setRecognitionLanguages_(languages)
                request.setRecognitionLevel_(0)  # Accurate (slower but best)
                request.setUsesLanguageCorrection_(True)

                handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(
                    image, {}
                )
                success, error = handler.performRequests_error_([request], None)
                if not success or error:
                    continue

                texts: list[str] = []
                scores: list[float] = []
                regions: list[OcrTextRegion] = []
                observations = request.results() or []
                for obs in observations:
                    top = obs.topCandidates_(1)
                    if top and len(top) > 0:
                        cc = top[0]
                        text = str(cc.string()).strip()
                        score = float(cc.confidence())
                        if text and score >= min_score:
                            texts.append(text)
                            scores.append(score)
                            # ★ v1.10.8 — bbox 추출 (Cocoa Bottom-Left → Top-Left 변환)
                            # Vision boundingBox: CGRect (origin=BL, normalized 0~1)
                            try:
                                bbox = obs.boundingBox()
                                bx = float(bbox.origin.x)
                                by_bl = float(bbox.origin.y)
                                bw = float(bbox.size.width)
                                bh = float(bbox.size.height)
                                # Top-Left origin 으로 변환: y_tl = 1 - (y_bl + h)
                                by_tl = max(0.0, 1.0 - (by_bl + bh))
                                regions.append(OcrTextRegion(
                                    text=text, score=round(score, 3),
                                    x=round(bx, 4), y=round(by_tl, 4),
                                    w=round(bw, 4), h=round(bh, 4),
                                ))
                            except Exception:
                                # bbox 추출 실패해도 text 는 유지 (regions 만 누락)
                                pass
            except Exception:
                # 한 frame 실패 → skip (전체 단계 실패 방지)
                continue

            if texts:
                entry = OcrLocalEntry(
                    i=cand.i,
                    t_abs=cand.t_abs,
                    text="\n".join(texts),
                    score=round(sum(scores) / len(scores), 3) if scores else 0.0,
                    regions=regions if regions else None,
                )
                entries.append(entry)
        finally:
            # autorelease pool drain — 이 frame 의 ObjC 객체 모두 release
            del pool

        # progress (대략 0.5초마다 emit)
        now = time.time()
        if on_progress and now - last_emit_at > 0.5:
            on_progress(AnalysisProgressEvent(
                step="ocr_local", status=StepStatus.RUNNING,
                percent=(idx + 1) / max(total, 1) * 100,
                message=f"{idx + 1}/{total} 후보 OCR (apple-vision)",
            ))
            last_emit_at = now

    return OcrLocalJson(
        method="apple-vision",
        min_score=min_score,
        candidate_count=len(entries),
        entries=entries,
    )


# ★ v1.9.3+ — _ocr_local() (PaddleOCR) 함수 완전 제거됨. Apple Vision 단독.
# git history 에서 옛 구현 참조 가능.


# =============================================================================
# 단계 6: face detection — mediapipe
# =============================================================================

# mediapipe 새 Tasks API 의 face detector 모델 (~200KB, 한 번만 다운로드)
_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)


def _default_face_model_path() -> Path:
    """모델 캐시 위치 — ~/Library/Application Support/showdon-yejja/models/."""
    return (
        Path.home() / "Library" / "Application Support" / "showdon-yejja"
        / "models" / "blaze_face_short_range.tflite"
    )


def _ensure_face_model(model_path: Path) -> None:
    """모델 없으면 curl 로 자동 다운로드 (~200KB)."""
    if model_path.exists():
        return
    model_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["curl", "-fsSL", _FACE_MODEL_URL, "-o", str(model_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not model_path.exists():
        raise AnalysisError_(
            f"face detector 모델 다운로드 실패: {r.stderr.strip()[:200]}"
        )


def _bbox_iou(b1: FaceBboxNorm, b2: FaceBboxNorm) -> float:
    """정규화 좌표 (0~1) bbox 의 IoU."""
    x1 = max(b1.x, b2.x)
    y1 = max(b1.y, b2.y)
    x2 = min(b1.x + b1.w, b2.x + b2.w)
    y2 = min(b1.y + b1.h, b2.y + b2.h)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = b1.w * b1.h + b2.w * b2.h - inter
    return inter / union if union > 0 else 0.0


def _cluster_faces_iou(
    frame_records: list[FrameFaces],
    iou_threshold: float = 0.4,
    max_gap_frames: int = 3,
) -> list[FaceCluster]:
    """인접 frame IoU 기반 Simple 클러스터링 (v1.3 NEW).

    같은 인물이 같은 위치에 연속 등장하면 같은 cluster_id.
    카메라 컷 끊기면 IoU 가 떨어져 새 cluster 시작 — v1.4 InsightFace 임베딩으로
    인물 동일성 매칭 가능.

    Returns:
        FaceCluster 리스트 (frame_count 내림차순).
        부수효과: frame_records[].faces[].cluster_id 가 채워짐.
    """
    next_cluster_id = 1
    # 활성 cluster: cluster_id → (last_seen_frame_idx, last_bbox)
    active: dict[int, tuple[int, FaceBboxNorm]] = {}
    # cluster 통계: id → {count, max_score, repr_frame}
    stats: dict[int, dict] = {}

    for frame_idx, fr in enumerate(frame_records):
        if not fr.faces:
            continue

        # 비활성 cluster 정리 (max_gap_frames 이상 안 보이면 만료)
        active = {
            cid: (last_idx, bb)
            for cid, (last_idx, bb) in active.items()
            if frame_idx - last_idx <= max_gap_frames
        }

        for face in fr.faces:
            best_cid = None
            best_iou = iou_threshold
            for cid, (_, bb) in active.items():
                iou = _bbox_iou(face.bbox_norm, bb)
                if iou > best_iou:
                    best_iou = iou
                    best_cid = cid

            if best_cid is None:
                best_cid = next_cluster_id
                next_cluster_id += 1
                stats[best_cid] = {
                    "count": 0,
                    "max_score": 0.0,
                    "repr_frame": fr.frame,
                }

            face.cluster_id = best_cid
            active[best_cid] = (frame_idx, face.bbox_norm)
            st = stats[best_cid]
            st["count"] += 1
            if face.score > st["max_score"]:
                st["max_score"] = face.score
                st["repr_frame"] = fr.frame

    return [
        FaceCluster(
            id=cid,
            frame_count=st["count"],
            representative_frame=st["repr_frame"],
            label=None,
        )
        for cid, st in sorted(stats.items(), key=lambda x: -x[1]["count"])
    ]


def _build_cast_list_candidates(
    faces: FaceClustersJson,
    *,
    top_n: int = 10,
    min_frame_count: int = 3,
) -> CastListCandidatesJson:
    """★ v1.10.9 — face_clusters.json 후처리. dominant cluster top N + 메타 추출.

    각 cluster 의 first_appearance / last_appearance / score_max 를 frame_records
    traversal 로 구함. faces.clusters 가 이미 frame_count 내림차순이라 top N slice.
    """
    cluster_meta: dict[int, dict] = {}
    for fr in faces.frames:
        for face in fr.faces:
            cid = face.cluster_id
            if cid is None:
                continue
            m = cluster_meta.get(cid)
            if m is None:
                cluster_meta[cid] = {
                    "first_t": fr.t,
                    "last_t": fr.t,
                    "max_score": face.score,
                }
            else:
                if fr.t < m["first_t"]:
                    m["first_t"] = fr.t
                if fr.t > m["last_t"]:
                    m["last_t"] = fr.t
                if face.score > m["max_score"]:
                    m["max_score"] = face.score

    candidates: list[CastListCandidate] = []
    for cl in faces.clusters[:top_n]:
        if cl.frame_count < min_frame_count:
            break
        m = cluster_meta.get(cl.id, {})
        candidates.append(CastListCandidate(
            cluster_id=cl.id,
            frame_count=cl.frame_count,
            score_max=round(m.get("max_score", 0.0), 3),
            first_appearance_t=round(m.get("first_t", 0.0), 2),
            last_appearance_t=round(m.get("last_t", 0.0), 2),
            representative_frame=cl.representative_frame,
        ))

    return CastListCandidatesJson(
        fps_target=faces.fps_target,
        total_clusters=len(faces.clusters),
        kept_top_n=len(candidates),
        candidates=candidates,
    )


def _faces(
    frames_dir: Path,
    *,
    fps_target: float = 1.0,
    model_path: Path | None = None,
    on_progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> FaceClustersJson:
    """mediapipe Tasks API (FaceDetector) + Simple IoU 클러스터링 (v1.3).

    클러스터링: 인접 frame bbox IoU > 0.4 → 같은 cluster_id 부여.
    더 정밀한 인물 동일성 매칭은 v1.4 InsightFace 로.
    모델은 첫 실행 시 자동 다운로드 후 캐시.
    """
    import cv2  # lazy
    import mediapipe as mp  # lazy
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    if model_path is None:
        model_path = _default_face_model_path()
    _ensure_face_model(model_path)

    base_options = mp_python.BaseOptions(model_asset_path=str(model_path))
    options = mp_vision.FaceDetectorOptions(
        base_options=base_options,
        min_detection_confidence=0.5,
    )
    # ★ v1.9.5 — try/finally 로 detector.close() 강제 (영상 batch 작업 시 GPU context 누수 회피)
    detector = mp_vision.FaceDetector.create_from_options(options)

    frames = sorted(frames_dir.glob("f_*.jpg"))
    frame_records: list[FrameFaces] = []
    total = len(frames)

    try:
        for i, fp in enumerate(frames):
            if is_cancelled and is_cancelled():
                raise AnalysisCancelled()

            # 1-indexed frame 의 시간 — fps=1.0 일 때 frame N → t = N - 1
            t = i / fps_target
            img = cv2.imread(str(fp))
            if img is None:
                continue

            h, w = img.shape[:2]
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = detector.detect(mp_image)

            faces_list: list[FaceDetection] = []
            for d in (result.detections or []):
                # 새 API: bounding_box (픽셀 단위) + categories[0].score
                bb = d.bounding_box
                score = float(d.categories[0].score) if d.categories else 0.0
                faces_list.append(FaceDetection(
                    bbox_norm=FaceBboxNorm(
                        x=round(bb.origin_x / w, 3) if w else 0.0,
                        y=round(bb.origin_y / h, 3) if h else 0.0,
                        w=round(bb.width / w, 3) if w else 0.0,
                        h=round(bb.height / h, 3) if h else 0.0,
                    ),
                    score=round(score, 3),
                    cluster_id=None,
                ))

            frame_records.append(FrameFaces(
                t=round(t, 2),
                frame=f"frames/{fp.name}",
                faces=faces_list,
            ))

            if on_progress and (i + 1) % 50 == 0:
                on_progress(AnalysisProgressEvent(
                    step="face_clusters", status=StepStatus.RUNNING,
                    percent=(i + 1) / max(total, 1) * 100.0,
                    message=f"{i + 1}/{total} 프레임",
                ))
    finally:
        # ★ v1.9.5 — mediapipe Metal GPU context 강제 release (영상 batch 시 누수 방지)
        try:
            detector.close()
        except Exception:
            pass

    frames_with_face = sum(1 for r in frame_records if r.faces)

    # v1.3 — IoU 기반 클러스터링 (frame_records[].faces[].cluster_id 채움)
    clusters = _cluster_faces_iou(frame_records)

    return FaceClustersJson(
        method="mediapipe-tasks-blaze_face_short_range+iou_cluster",
        fps_target=fps_target,
        frame_count=len(frame_records),
        frames_with_face=frames_with_face,
        frames=frame_records,
        clusters=clusters,
    )


# =============================================================================
# Orchestrator
# =============================================================================

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _enum_value(v: Any) -> Any:
    return v.value if isinstance(v, Enum) else v


def analyze_video(
    video_path: Path | str,
    output_dir: Path | str,
    *,
    config: Optional[AppConfig] = None,
    on_progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
    source_meta_path: Path | str | None = None,
    hf_home: Path | str | None = None,
) -> AnalysisMeta:
    """영상 1편 분석 — 모든 단계 순차 실행.

    Args:
        video_path: 원본 영상 mp4
        output_dir: {data_root}/<채널>/분석/<영상명>/
        config: 설정 (없으면 기본값). stt_model, scene_threshold, fps 등.
        on_progress: 단계별 진행률 콜백
        is_cancelled: 취소 체크 콜백 (각 단계 시작 시 + STT/face 안에서 체크)
        source_meta_path: ~/showdon/yejjas/<채널>/원본/<영상명>/source_meta.json — 있으면 meta.source 에 매핑
        hf_home: faster-whisper 모델 캐시 위치 (HF_HOME 환경변수)

    Returns:
        AnalysisMeta — 최종 상태 (status: completed | partial | failed)

    Raises:
        AnalysisCancelled: 사용자 취소
        그 외 예외: ffprobe 실패 시 (치명적)
    """
    config = config or AppConfig()
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 시작 마커 정리 (재실행 대응)
    clear_status_markers(output_dir)

    # 메타 초기화
    meta = AnalysisMeta(
        video_path=str(video_path),
        video_filename=video_path.name,
        video_basename=video_path.stem,
        video_size_bytes=video_path.stat().st_size if video_path.exists() else 0,
    )
    meta.analysis.started_at = _now_iso()
    meta.analysis.status = AnalysisStatus.RUNNING

    # source_meta 통합 (다운로드된 영상이면 출처 정보 흡수)
    if source_meta_path:
        sp = Path(source_meta_path)
        if sp.exists():
            try:
                meta.source = load_dataclass(SourceMeta, sp)
            except Exception:
                pass  # source_meta 로딩 실패는 치명적이지 않음

    meta_path = output_dir / "meta.json"
    save_dataclass(meta, meta_path)

    def emit(step: str, status: StepStatus, percent: float = 0.0,
             message: str = "", elapsed: float = 0.0) -> None:
        if on_progress:
            on_progress(AnalysisProgressEvent(
                step=step, status=status, percent=percent,
                message=message, elapsed_s=elapsed,
            ))

    def update_step(step_name: str, status: StepStatus,
                    elapsed_s: float = 0.0, **extras: Any) -> None:
        meta.analysis.steps[step_name] = {
            "status": _enum_value(status),
            "elapsed_s": round(elapsed_s, 1),
            **{k: _enum_value(v) for k, v in extras.items()},
        }
        save_dataclass(meta, meta_path)

    def record_error(step: str, msg: str) -> None:
        meta.analysis.errors.append(AnalysisError(
            step=step, message=msg, timestamp=_now_iso(),
        ))

    def cancel_check() -> None:
        if is_cancelled and is_cancelled():
            raise AnalysisCancelled()

    failed_steps: list[str] = []

    try:
        # ====================================================================
        # 1. ffprobe — 치명적 실패 시 전체 중단
        # ====================================================================
        emit("ffprobe", StepStatus.RUNNING)
        t0 = time.time()
        try:
            video_info, audio_info, duration_s, size_bytes = _ffprobe(video_path)
            meta.video = video_info
            meta.audio = audio_info
            meta.video_duration_s = duration_s
            meta.video_size_bytes = size_bytes or meta.video_size_bytes
            update_step("ffprobe", StepStatus.COMPLETED, time.time() - t0)
            emit("ffprobe", StepStatus.COMPLETED, 100.0,
                 f"{video_info.width}x{video_info.height} {duration_s:.1f}s",
                 time.time() - t0)
        except Exception as e:
            update_step("ffprobe", StepStatus.FAILED, time.time() - t0,
                        error=str(e))
            record_error("ffprobe", str(e))
            emit("ffprobe", StepStatus.FAILED, 0, str(e))
            raise  # 치명적

        cancel_check()

        # ====================================================================
        # 1b. 자막 감지 + transcript_source.json (★ v1.9.5 NEW)
        # 실패해도 STT/OCR/face 는 그대로 진행 (선택적 보강 단계)
        # ★ v1.9.6 — transcript_source.preference 가 frame_extraction step 의
        #   하이브리드 fps 분기에 사용되므로 변수 None 초기화 (try 실패 대응)
        # ====================================================================
        transcript_source = None
        emit("transcript_source", StepStatus.RUNNING)
        t0 = time.time()
        try:
            transcript_source = _detect_and_copy_transcripts(video_path, output_dir)
            ts_path = output_dir / "transcript_source.json"
            save_dataclass(transcript_source, ts_path)
            n = len(transcript_source.sources)
            pref = transcript_source.preference
            update_step("transcript_source", StepStatus.COMPLETED, time.time() - t0,
                        sources=n, preference=pref)
            emit("transcript_source", StepStatus.COMPLETED, 100.0,
                 f"{n}개 자막 / preference={pref}", time.time() - t0)
        except Exception as e:
            # 실패해도 치명적 X — 다음 step 진행
            update_step("transcript_source", StepStatus.FAILED, time.time() - t0,
                        error=str(e))
            record_error("transcript_source", str(e))
            emit("transcript_source", StepStatus.FAILED, 0, str(e))

        cancel_check()

        # ====================================================================
        # 2. STT — 실패 시 _PARTIAL
        # ====================================================================
        stt: Optional[SttJson] = None
        emit("stt", StepStatus.RUNNING)
        t0 = time.time()
        try:
            stt_path = output_dir / "stt.json"
            if stt_path.exists():
                stt = load_dataclass(SttJson, stt_path)
            elif not meta.audio.present:
                update_step("stt", StepStatus.SKIPPED, 0,
                            reason="audio 트랙 없음")
                failed_steps.append("stt")
            else:
                def stt_cb(evt: AnalysisProgressEvent) -> None:
                    if on_progress:
                        evt.elapsed_s = time.time() - t0
                        on_progress(evt)

                # ★ v1.9.3+ — STT 엔진 mlx-whisper 단독 (faster-whisper 제거됨, 검증: 6.6x ↑)
                stt = _stt_mlx(
                    video_path,
                    mlx_model_repo=getattr(
                        config, "stt_mlx_model_repo",
                        "mlx-community/whisper-large-v3-mlx",
                    ),
                    language=config.stt_language,
                    initial_prompt=getattr(config, "stt_initial_prompt", "") or "",
                    duration_s=duration_s,
                    on_progress=stt_cb,
                    is_cancelled=is_cancelled,
                    temperature=getattr(config, "stt_temperature", 0.0),
                    condition_on_previous=getattr(
                        config, "stt_condition_on_previous", False,
                    ),
                )
                save_dataclass(stt, stt_path)

            if stt:
                update_step("stt", StepStatus.COMPLETED, time.time() - t0,
                            model=stt.model, segment_count=stt.segment_count)
                emit("stt", StepStatus.COMPLETED, 100.0,
                     f"{stt.segment_count} 세그먼트", time.time() - t0)
        except AnalysisCancelled:
            raise
        except Exception as e:
            update_step("stt", StepStatus.FAILED, time.time() - t0, error=str(e))
            record_error("stt", str(e))
            emit("stt", StepStatus.FAILED, 0, str(e))
            failed_steps.append("stt")

        cancel_check()

        # ====================================================================
        # 3. Scene detection
        # ====================================================================
        scenes: Optional[SceneCutsJson] = None
        emit("scene_detection", StepStatus.RUNNING)
        t0 = time.time()
        try:
            scenes_path = output_dir / "scene_cuts.json"
            if scenes_path.exists():
                scenes = load_dataclass(SceneCutsJson, scenes_path)
            else:
                scenes = _scenes(video_path,
                                 threshold=config.scene_threshold,
                                 duration_s=duration_s,
                                 is_cancelled=is_cancelled)
                save_dataclass(scenes, scenes_path)

            update_step("scene_detection", StepStatus.COMPLETED, time.time() - t0,
                        threshold=config.scene_threshold,
                        cut_count=scenes.cut_count)
            emit("scene_detection", StepStatus.COMPLETED, 100.0,
                 f"{scenes.cut_count} 컷", time.time() - t0)
        except AnalysisCancelled:
            raise
        except Exception as e:
            update_step("scene_detection", StepStatus.FAILED, time.time() - t0,
                        error=str(e))
            record_error("scene_detection", str(e))
            emit("scene_detection", StepStatus.FAILED, 0, str(e))
            failed_steps.append("scene_detection")

        cancel_check()

        # ====================================================================
        # 4. Frame extraction
        # ★ v1.9.6 — 하이브리드 fps 분기 (transcript_source.preference 기반)
        #   - manual srt 있음 → config.frame_extraction_fps 그대로 (1.0 디폴트, 시간 절약)
        #   - manual 없음 → 2배 정밀 (0.5초당 1프레임 = fps 2.0). 화자 매칭 / OCR 시각 / scene 정밀도 ↑
        # 같은 effective_fps 를 ocr_candidates + face_clusters 에도 전달 (3 단계 정합)
        # ====================================================================
        _ts_pref = transcript_source.preference if transcript_source else "none"
        effective_fps = (
            config.frame_extraction_fps
            if _ts_pref == "manual"
            else config.frame_extraction_fps * 2.0
        )

        n_frames = 0
        frames_dir = output_dir / "frames"
        emit("frame_extraction", StepStatus.RUNNING)
        t0 = time.time()
        try:
            n_frames = _extract_frames(
                video_path, frames_dir,
                fps_target=effective_fps,
                scale_w=config.frame_scale_w,
                is_cancelled=is_cancelled,
            )
            update_step("frame_extraction", StepStatus.COMPLETED, time.time() - t0,
                        fps=effective_fps, base_fps=config.frame_extraction_fps,
                        transcript_preference=_ts_pref, frame_count=n_frames)
            emit("frame_extraction", StepStatus.COMPLETED, 100.0,
                 f"{n_frames} 프레임 (fps={effective_fps}, pref={_ts_pref})", time.time() - t0)
        except AnalysisCancelled:
            raise
        except Exception as e:
            update_step("frame_extraction", StepStatus.FAILED, time.time() - t0,
                        error=str(e))
            record_error("frame_extraction", str(e))
            emit("frame_extraction", StepStatus.FAILED, 0, str(e))
            failed_steps.append("frame_extraction")

        cancel_check()

        # ====================================================================
        # 5. OCR candidates — STT + scene + frames 모두 있어야 의미 있음
        # ====================================================================
        emit("ocr_candidates", StepStatus.RUNNING)
        t0 = time.time()
        try:
            if not stt or n_frames <= 0:
                update_step("ocr_candidates", StepStatus.SKIPPED, 0,
                            reason="STT 또는 frames 미존재")
                failed_steps.append("ocr_candidates")
                emit("ocr_candidates", StepStatus.SKIPPED, 0,
                     "STT/frames 부족")
            else:
                ocr = _build_ocr_candidates(
                    stt, scenes, n_frames, effective_fps
                )
                save_dataclass(ocr, output_dir / "ocr_candidates.json")
                update_step("ocr_candidates", StepStatus.COMPLETED, time.time() - t0,
                            candidate_count=ocr.candidate_count)
                emit("ocr_candidates", StepStatus.COMPLETED, 100.0,
                     f"{ocr.candidate_count} 후보", time.time() - t0)
        except Exception as e:
            update_step("ocr_candidates", StepStatus.FAILED, time.time() - t0,
                        error=str(e))
            record_error("ocr_candidates", str(e))
            emit("ocr_candidates", StepStatus.FAILED, 0, str(e))
            failed_steps.append("ocr_candidates")

        cancel_check()

        # ====================================================================
        # 5b. 로컬 OCR 더블체킹 (옵션, default ON) — depends on ocr_candidates
        # ====================================================================
        ocr_local_path = output_dir / "ocr_local.json"
        if not config.enable_local_ocr:
            update_step("ocr_local", StepStatus.SKIPPED, 0,
                        reason="config.enable_local_ocr=False")
            emit("ocr_local", StepStatus.SKIPPED, 0, "비활성")
        else:
            emit("ocr_local", StepStatus.RUNNING)
            t0 = time.time()
            try:
                ocr_path = output_dir / "ocr_candidates.json"
                if not ocr_path.exists() or n_frames <= 0:
                    update_step("ocr_local", StepStatus.SKIPPED, 0,
                                reason="ocr_candidates 또는 frames 미존재")
                    failed_steps.append("ocr_local")
                    emit("ocr_local", StepStatus.SKIPPED, 0,
                         "ocr_candidates/frames 부족")
                elif ocr_local_path.exists():
                    ocr_local = load_dataclass(OcrLocalJson, ocr_local_path)
                    update_step("ocr_local", StepStatus.COMPLETED,
                                time.time() - t0,
                                method=ocr_local.method,
                                entry_count=ocr_local.candidate_count,
                                cached=True)
                    emit("ocr_local", StepStatus.COMPLETED, 100.0,
                         f"기존 결과 재사용 ({ocr_local.candidate_count}항목)",
                         time.time() - t0)
                else:
                    cands = load_dataclass(OcrCandidatesJson, ocr_path)

                    def ocr_cb(evt: AnalysisProgressEvent) -> None:
                        if on_progress:
                            evt.elapsed_s = time.time() - t0
                            on_progress(evt)

                    # ★ v1.9.3+ — Apple Vision 단독 (PaddleOCR 제거됨)
                    ocr_local = _ocr_local_apple_vision(
                        cands, output_dir,
                        min_score=config.local_ocr_min_score,
                        on_progress=ocr_cb,
                        is_cancelled=is_cancelled,
                    )
                    save_dataclass(ocr_local, ocr_local_path)
                    update_step("ocr_local", StepStatus.COMPLETED,
                                time.time() - t0,
                                method=ocr_local.method,
                                entry_count=ocr_local.candidate_count)
                    emit("ocr_local", StepStatus.COMPLETED, 100.0,
                         f"{ocr_local.candidate_count} 항목 OCR",
                         time.time() - t0)
            except AnalysisCancelled:
                raise
            except ImportError as e:
                # paddleocr 미설치 — requirements-optional.txt 참조
                update_step("ocr_local", StepStatus.SKIPPED, 0,
                            reason=f"paddleocr 미설치: {e}")
                emit("ocr_local", StepStatus.SKIPPED, 0,
                     "paddleocr 미설치 (requirements-optional.txt)")
                # SKIPPED 는 failed_steps 에 안 넣음 — 정상 흐름의 옵션 기능
            except Exception as e:
                update_step("ocr_local", StepStatus.FAILED,
                            time.time() - t0, error=str(e))
                record_error("ocr_local", str(e))
                emit("ocr_local", StepStatus.FAILED, 0, str(e))
                failed_steps.append("ocr_local")

        cancel_check()

        # ====================================================================
        # 6. Face detection
        # ====================================================================
        emit("face_clusters", StepStatus.RUNNING)
        t0 = time.time()
        try:
            if n_frames <= 0:
                update_step("face_clusters", StepStatus.SKIPPED, 0,
                            reason="frames 미존재")
                failed_steps.append("face_clusters")
                emit("face_clusters", StepStatus.SKIPPED, 0, "frames 부족")
            else:
                def face_cb(evt: AnalysisProgressEvent) -> None:
                    if on_progress:
                        evt.elapsed_s = time.time() - t0
                        on_progress(evt)

                faces = _faces(frames_dir,
                               fps_target=effective_fps,
                               on_progress=face_cb,
                               is_cancelled=is_cancelled)
                save_dataclass(faces, output_dir / "face_clusters.json")
                # ★ v1.10.9 — cast_list_candidates.json 자동 저장 (모드 A cast 매핑 input)
                try:
                    cast_candidates = _build_cast_list_candidates(faces)
                    save_dataclass(cast_candidates,
                                   output_dir / "cast_list_candidates.json")
                except Exception as _cc_err:  # noqa: BLE001 — cast_list 실패는 analysis 막지 X
                    record_error("cast_list_candidates", str(_cc_err))
                update_step("face_clusters", StepStatus.COMPLETED, time.time() - t0,
                            cluster_count=len(faces.clusters),
                            frames_with_face=faces.frames_with_face)
                emit("face_clusters", StepStatus.COMPLETED, 100.0,
                     f"{faces.frames_with_face}/{n_frames} 프레임에 얼굴",
                     time.time() - t0)
        except AnalysisCancelled:
            raise
        except Exception as e:
            update_step("face_clusters", StepStatus.FAILED, time.time() - t0,
                        error=str(e))
            record_error("face_clusters", str(e))
            emit("face_clusters", StepStatus.FAILED, 0, str(e))
            failed_steps.append("face_clusters")

        # ====================================================================
        # 마무리
        # ====================================================================
        meta.analysis.finished_at = _now_iso()
        try:
            started = datetime.fromisoformat(meta.analysis.started_at)
            finished = datetime.fromisoformat(meta.analysis.finished_at)
            meta.analysis.duration_s = (finished - started).total_seconds()
        except Exception:
            pass

        if not failed_steps:
            meta.analysis.status = AnalysisStatus.COMPLETED
            save_dataclass(meta, meta_path)
            write_marker(output_dir, MARKER_DONE)
        else:
            meta.analysis.status = AnalysisStatus.PARTIAL
            save_dataclass(meta, meta_path)
            write_marker(output_dir, MARKER_PARTIAL,
                         {"failed_steps": failed_steps,
                          "finished_at": meta.analysis.finished_at})

        return meta

    except AnalysisCancelled:
        meta.analysis.status = AnalysisStatus.FAILED
        meta.analysis.finished_at = _now_iso()
        record_error("orchestrator", "사용자 취소")
        save_dataclass(meta, meta_path)
        # 취소는 마커 안 남김 (재시도 시 깨끗이)
        raise

    except Exception as e:
        meta.analysis.status = AnalysisStatus.FAILED
        meta.analysis.finished_at = _now_iso()
        record_error("orchestrator", str(e))
        save_dataclass(meta, meta_path)
        write_marker(output_dir, MARKER_FAILED,
                     {"error": str(e), "finished_at": meta.analysis.finished_at})
        raise


# =============================================================================
# 스모크 테스트 — 헤비 deps 없이 검증 가능한 부분만
# =============================================================================

if __name__ == "__main__":
    import sys
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    print(f"=== analyze.py 스모크 테스트 ===")

    # 1. _frame_path_for_time
    print("\n[1] _frame_path_for_time")
    cases = [
        (0.5, 100, 1.0, "frames/f_00001.jpg"),
        (1.5, 100, 1.0, "frames/f_00002.jpg"),
        (4.5, 100, 1.0, "frames/f_00005.jpg"),
        (99.9, 100, 1.0, "frames/f_00100.jpg"),
        (1000.0, 100, 1.0, "frames/f_00100.jpg"),  # cap
        (0.0, 100, 1.0, "frames/f_00001.jpg"),     # min
    ]
    for t, n, fps, expected in cases:
        got = _frame_path_for_time(t, n, fps)
        assert got == expected, f"t={t} → {got}, expected {expected}"
    print(f"  [OK] {len(cases)} 케이스")

    # 2. _build_ocr_candidates — STT + scene + gap 통합
    print("\n[2] _build_ocr_candidates — STT 만")
    stt = SttJson(
        model="test", duration_s=10.0, segment_count=2,
        segments=[
            SttSegment(id=1, start=0.0, end=2.0, text="안녕하세요"),
            SttSegment(id=2, start=4.0, end=6.0, text="반갑습니다"),
        ],
    )
    ocr = _build_ocr_candidates(stt, None, frame_count=10, fps_target=1.0)
    # STT 2개 + gap 1개 (2.0~4.0 사이) = 3개
    assert ocr.candidate_count == 3, f"got {ocr.candidate_count}"
    kinds = [c.kind for c in ocr.candidates]
    assert kinds.count(CandidateKind.STT) == 2
    assert kinds.count(CandidateKind.GAP) == 1
    # 시간 순
    times = [c.t_abs for c in ocr.candidates]
    assert times == sorted(times)
    # 프레임 매칭
    for c in ocr.candidates:
        assert c.frame.startswith("frames/f_")
    print(f"  [OK] STT 2개 + gap 1개 = 3 candidates, 시간 정렬, frame 매칭")

    print("\n[3] _build_ocr_candidates — STT + scene 통합 (dedup)")
    scenes = SceneCutsJson(cut_count=4, cut_times=[0.5, 2.5, 5.0, 8.0])
    # 0.5 는 STT(1) 의 t (start+0.5=0.5) 와 정확히 같음 → 제외
    # 2.5 는 gap (2.0+0.5=2.5) 와 같음 → 제외
    # 5.0 도 STT(2) 의 t (4.0+0.5=4.5) 와 0.5초 차이 → 안 가까움 (near_dedup=0.3) → 포함
    # 8.0 새 시점 → 포함
    ocr2 = _build_ocr_candidates(stt, scenes, frame_count=10, fps_target=1.0)
    scene_count = sum(1 for c in ocr2.candidates if c.kind == CandidateKind.SCENE)
    assert scene_count == 2, f"scene = {scene_count}, expected 2 (5.0, 8.0)"
    print(f"  [OK] scene dedup: 4개 중 0.5/2.5 제외 → 2개 포함")

    print("\n[4] _build_ocr_candidates — STT 없을 때 (Scene 만)")
    ocr3 = _build_ocr_candidates(None, scenes, frame_count=10, fps_target=1.0)
    assert ocr3.candidate_count == 4
    assert all(c.kind == CandidateKind.SCENE for c in ocr3.candidates)
    print(f"  [OK] STT 없으면 scene 만 4개")

    print("\n[5] AnalysisProgressEvent dataclass")
    evt = AnalysisProgressEvent(step="stt", status=StepStatus.RUNNING,
                                percent=42.0, message="테스트")
    assert evt.step == "stt"
    assert evt.status == StepStatus.RUNNING
    print("  [OK]")

    print("\n=== 모든 스모크 테스트 통과 ===")
    print("\n실제 분석 테스트는 venv + ffmpeg + faster_whisper + mediapipe 가 깔린 환경에서:")
    print("  python -c 'from backend.analyze import analyze_video; "
          "analyze_video(\"test.mp4\", \"./out\", on_progress=print)'")
