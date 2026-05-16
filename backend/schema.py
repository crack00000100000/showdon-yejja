# -*- coding: utf-8 -*-
"""
backend/schema.py — PRD §3 데이터 포맷의 dataclass 정의.

이 모듈은 코워크 ↔ 로컬 사이의 계약(JSON 스키마)을 코드로 박는다.
스키마 버전: 1.1 (어덴덤 적용)

변경 시 주의:
- schema_version 을 올리고 양쪽 챗(설계·구현) 모두 업데이트
- 기존 필드 의미 변경 시 major 버전 (1.x → 2.0)
- 필드 추가만 하고 기존 의미 유지면 minor 버전 (1.1 → 1.2)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Type, TypeVar, Union, get_args, get_origin, get_type_hints


# =============================================================================
# 상수
# =============================================================================

SCHEMA_VERSION = "1.2"

# 마커 파일 (PRD §2.1)
MARKER_DONE = "_DONE"            # 분석/완성 — 모든 단계 성공
MARKER_READY = "_READY"          # 편집점 — 코워크가 모든 파일 작성 완료
MARKER_FAILED = "_FAILED"        # 분석 — 전체 실패
MARKER_PARTIAL = "_PARTIAL"      # 분석 — 일부 단계만 성공


# =============================================================================
# Enum 정의
# =============================================================================

class CandidateKind(str, Enum):
    """OCR 후보 시점 종류 (PRD §3.4)."""
    STT = "stt"            # STT 발화 시점
    GAP = "gap"            # 발화 빈 구간
    SCENE = "scene"        # scene change 시점


class AnalysisStatus(str, Enum):
    """분석 전체 상태 (PRD §3.1)."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class StepStatus(str, Enum):
    """분석 단계별 상태 (PRD §3.1)."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FolderMetaKind(str, Enum):
    """폴더 메타 종류 (PRD §3.8)."""
    ANALYSIS_META = "analysis_meta"
    EDIT_PLAN_META = "edit_plan_meta"
    PRODUCED_META = "produced_meta"


class ReanalysisPolicy(str, Enum):
    """이미 분석된 영상 재추가 시 동작."""
    ASK = "ask"
    OVERWRITE = "overwrite"
    SKIP = "skip"


class SleepPrevention(str, Enum):
    """슬립 방지 정책."""
    DURING_QUEUE = "during_queue"
    ALWAYS = "always"
    NEVER = "never"


# =============================================================================
# 0. SourceMeta — 다운로드 메타 (v1.1 NEW, 어덴덤)
# =============================================================================

@dataclass
class SourceMeta:
    """원본/<영상명>/source_meta.json — yt-dlp 메타 정제본.

    1단계 분석이 읽어가서 분석 폴더 meta.json 의 source 섹션에 매핑.
    4단계 편집의 footer.source_text 도 여기서 자동 생성:
        "출처 - {uploader}"
    """
    schema_version: str = SCHEMA_VERSION
    url: str = ""
    platform: str = "unknown"               # youtube, tiktok, instagram, ...
    uploader: str = ""                      # 출처 표기에 사용 (예: "유병재")
    uploader_handle: Optional[str] = None   # @yubyungjae
    channel_url: Optional[str] = None
    title: str = ""
    upload_date: Optional[str] = None       # ISO 8601 (YYYY-MM-DD)
    duration_s: float = 0.0
    downloaded_at: str = ""                 # ISO 8601 timestamp


# =============================================================================
# 1. meta.json — 분석 메타 + 진행 상태 (PRD §3.1)
# =============================================================================

@dataclass
class VideoInfo:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""
    bit_rate: int = 0
    aspect_ratio: str = ""                  # "16:9", "9:16", ...


@dataclass
class AudioInfo:
    present: bool = False
    codec: Optional[str] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None


@dataclass
class AnalysisError:
    step: str
    message: str
    timestamp: str


@dataclass
class AnalysisProgress:
    """단계별 진행 상황. steps 는 자유 dict — step 이름 → {status, elapsed_s, ...}."""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_s: float = 0.0
    status: AnalysisStatus = AnalysisStatus.QUEUED
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[AnalysisError] = field(default_factory=list)


@dataclass
class AnalysisMeta:
    """{data_root}/<채널>/분석/<영상명>/meta.json"""
    schema_version: str = SCHEMA_VERSION
    video_path: str = ""
    video_filename: str = ""
    video_basename: str = ""
    video_size_bytes: int = 0
    video_duration_s: float = 0.0
    video: VideoInfo = field(default_factory=VideoInfo)
    audio: AudioInfo = field(default_factory=AudioInfo)
    analysis: AnalysisProgress = field(default_factory=AnalysisProgress)
    # v1.1 — 다운로드 통합. 1단계가 source_meta.json 을 여기로 복사
    source: Optional[SourceMeta] = None


# =============================================================================
# 2. stt.json — faster-whisper 출력 (PRD §3.2)
# =============================================================================

@dataclass
class SttWord:
    start: float
    end: float
    word: str
    probability: float = 1.0


@dataclass
class SttSegment:
    id: int
    start: float
    end: float
    text: str
    no_speech_probability: float = 0.0
    avg_logprob: float = 0.0
    compression_ratio: float = 0.0
    words: list[SttWord] = field(default_factory=list)


@dataclass
class SttJson:
    schema_version: str = SCHEMA_VERSION
    model: str = ""                         # "faster-whisper-large-v3"
    compute_type: str = ""                  # "int8", "float16", ...
    language: str = "ko"
    language_probability: float = 0.0
    duration_s: float = 0.0
    segment_count: int = 0
    vad_filter: bool = True
    segments: list[SttSegment] = field(default_factory=list)


# =============================================================================
# 3. scene_cuts.json — ffmpeg scene detection (PRD §3.3)
# =============================================================================

@dataclass
class SceneCutsJson:
    schema_version: str = SCHEMA_VERSION
    method: str = "ffmpeg-scene-detection"
    threshold: float = 0.3
    cut_count: int = 0
    cut_times: list[float] = field(default_factory=list)


# =============================================================================
# 4. ocr_candidates.json — OCR 후보 시점 (PRD §3.4) ★ 핵심
# =============================================================================

@dataclass
class OcrCandidate:
    i: int                                  # 1부터 시작
    t_abs: float                            # 영상 절대 시각 (초)
    kind: CandidateKind
    frame: str                              # "frames/f_00001.jpg" (상대경로)
    stt_segment_id: Optional[int] = None    # kind="stt" 일 때만
    stt_text_hint: str = ""                 # kind="stt" 일 때 STT 텍스트


@dataclass
class OcrCandidatesJson:
    schema_version: str = SCHEMA_VERSION
    candidate_count: int = 0
    fps_target: float = 1.0
    candidates: list[OcrCandidate] = field(default_factory=list)


# =============================================================================
# 4b. ocr_local.json — 로컬 PaddleOCR 더블체킹 (v1.1 NEW, 어덴덤)
# =============================================================================

@dataclass
class OcrTextRegion:
    """★ v1.10.8 NEW — Apple Vision VNRecognizedTextObservation 결과 (text + bbox).

    bbox 는 *Top-Left origin 정규화 좌표* (0~1). Vision API 의 Cocoa Bottom-Left
    origin 을 analyze.py 에서 변환해 저장.

    hide_caption mask 작업의 input — dialog substring 일치 자체 자막의 bbox 만 mask 후보.
    """
    text: str
    score: float
    x: float
    y: float
    w: float
    h: float


@dataclass
class OcrLocalEntry:
    """ocr_candidates 의 i 와 1:1 매칭. 코워크 Claude Vision OCR 과 cross-check 용."""
    i: int                              # ocr_candidates.json 의 i
    t_abs: float                        # 영상 절대 시각 (초)
    text: str                           # 합쳐진 text (여러 줄은 \n 결합)
    score: float = 0.0                  # 평균 신뢰도 (0~1)
    # ★ v1.10.8 NEW — text 별 bbox detail. 기존 영상은 None (graceful fallback)
    regions: Optional[list[OcrTextRegion]] = None


@dataclass
class OcrLocalJson:
    """{data_root}/<채널>/분석/<영상명>/ocr_local.json — PaddleOCR korean 결과.

    코워크 Claude 가 frame 을 Vision OCR 한 결과와 비교해 cross-check.
    두 결과 일치하면 그대로, 불일치하면 자연스러운 쪽 채택 + 사용자 알림.
    같은 텍스트가 여러 candidate 에 반복되면 고정 텍스트(채널 워터마크 등)로 자동 분류 가능.
    """
    schema_version: str = SCHEMA_VERSION
    method: str = "paddleocr-korean"
    min_score: float = 0.5
    candidate_count: int = 0
    entries: list[OcrLocalEntry] = field(default_factory=list)


# =============================================================================
# 4c. transcript_source.json — yt-dlp 자막 메타 (★ v1.9.5 NEW)
# =============================================================================

@dataclass
class TranscriptSourceLang:
    """자막 1개 (언어별 manual 또는 auto)."""
    lang: str                          # "ko" | "en" | ...
    type: str                          # "manual" | "auto"
    path: str                          # 분석 폴더 기준 상대경로 (예: "subs/ko.srt")
    segment_count: int = 0             # srt segment 개수 (디버그용)


@dataclass
class TranscriptSourceJson:
    """{data_root}/<채널>/분석/<영상명>/transcript_source.json — ★ v1.9.5 NEW.

    yt-dlp 가 영상과 같이 받은 자막 메타. info.json 의 subtitles / automatic_captions
    키로 manual / auto 정확히 구별. 분석 폴더 subs/ 로 자막 파일 복사하여 결과 폴더
    이주 시 손실 방지.

    코워크 Claude 가 dialog 작성 시 우선순위 결정:
      - preference == "manual" → dialog 텍스트는 manual srt / timing/비언어는 STT (하이브리드)
      - preference == "auto"   → STT 우선 / auto srt 는 보조 reference
      - preference == "none"   → STT + OCR 표준 흐름 (v1.9.4 그대로)
    """
    schema_version: str = SCHEMA_VERSION
    sources: list[TranscriptSourceLang] = field(default_factory=list)
    preference: str = "none"           # "manual" | "auto" | "none"
    info_json_relpath: Optional[str] = None  # debug 용 — info.json 원본 위치


# =============================================================================
# 5. face_clusters.json — mediapipe 얼굴 클러스터 (PRD §3.5)
# =============================================================================

@dataclass
class FaceBboxNorm:
    """정규화 좌표 (0~1, 영상 크기 비율)."""
    x: float
    y: float
    w: float
    h: float


@dataclass
class FaceDetection:
    bbox_norm: FaceBboxNorm
    score: float
    cluster_id: Optional[int] = None


@dataclass
class FrameFaces:
    t: float                                # 절대 시각
    frame: str                              # 상대 경로
    faces: list[FaceDetection] = field(default_factory=list)


@dataclass
class FaceCluster:
    """단순 클러스터링 결과. v1.0 정확도 낮을 수 있음 (PRD §3.5 주의)."""
    id: int
    frame_count: int = 0
    label: Optional[str] = None             # v1.0 = None, v1.1 사용자 라벨링
    representative_frame: str = ""


@dataclass
class FaceClustersJson:
    schema_version: str = SCHEMA_VERSION
    method: str = "mediapipe-face-detection-v1"
    fps_target: float = 1.0
    frame_count: int = 0
    frames_with_face: int = 0
    frames: list[FrameFaces] = field(default_factory=list)
    clusters: list[FaceCluster] = field(default_factory=list)


# =============================================================================
# 5b. cast_list_candidates.json — face_cluster top N + 메타 (★ v1.10.9 NEW)
# =============================================================================

@dataclass
class CastListCandidate:
    """★ v1.10.9 — face_clusters 의 dominant cluster top N. cast_list.json 후보 input."""
    cluster_id: int
    frame_count: int                  # cluster 의 등장 frame 수
    score_max: float                  # cluster 안 face score 최대 (0~1)
    first_appearance_t: float         # 첫 등장 시각 (초)
    last_appearance_t: float          # 마지막 등장 시각 (초)
    representative_frame: str         # frames/f_NNNNN.jpg (대표 jpg 경로)


@dataclass
class CastListCandidatesJson:
    """{data_root}/<채널>/분석/<영상명>/cast_list_candidates.json — ★ v1.10.9 NEW.

    face_clusters 단계 후처리. backend 가 dominant cluster 상위 N개 (frame_count 기준)
    를 추출해서 저장. 코워크 모드 A 가 read 해서 representative_frame jpg + source_meta
    description/title + web_search 로 cluster_id → name 매핑 후 cast_list.json 저장
    (사용자 review = 모드 A 후보 응답에 cast_list section 통합).
    """
    schema_version: str = SCHEMA_VERSION
    kind: str = "cast_list_candidates"
    fps_target: float = 1.0
    total_clusters: int = 0           # 전체 cluster 개수 (top N 으로 자르기 전)
    kept_top_n: int = 10
    candidates: list[CastListCandidate] = field(default_factory=list)


# =============================================================================
# 5c. cast_list.json — cluster_id → name 매핑 (★ v1.10.9 NEW)
# =============================================================================

@dataclass
class CastListEntry:
    """★ v1.10.9 — cluster_id 와 매핑된 인물 정보. 코워크 모드 A 가 채움."""
    cluster_id: int
    name: str                         # 실명 또는 fallback ("게스트1", "MC", "이모님" 등)
    aliases: list[str] = field(default_factory=list)
    role: str = ""                    # 메인 / 게스트 / MC / 사회자 / 기타
    confidence: float = 0.0           # 0~1 (모드 A 의 매핑 확신도)
    representative_frame: str = ""    # frames/f_NNNNN.jpg


@dataclass
class CastListJson:
    """{data_root}/<채널>/분석/<영상명>/cast_list.json — ★ v1.10.9 NEW.

    코워크 모드 A 가 cast_list_candidates.json 보고 매핑 후 저장. backend 직접 안 만듦.
    explain_validator / edit_plan_validator 등이 §9.5 인물명 grep ground truth 로 활용.

    legacy 형식 (clusters[] 또는 list of {name, aliases}) 은 explain_validator 가
    graceful read — 새 형식 (entries[]) 와 둘 다 지원 (이전 영상 호환).
    """
    schema_version: str = SCHEMA_VERSION
    kind: str = "cast_list"
    entries: list[CastListEntry] = field(default_factory=list)


# =============================================================================
# 6. edit_plan.json — 3단계 출력 (PRD §3.6) ★ 핵심
# =============================================================================

@dataclass
class EditPlanSource:
    video_path: str = ""
    analysis_dir: str = ""


@dataclass
class EditPlanCandidate:
    id: int = 0
    title_for_dir: str = ""                 # 안전화된 제목 (한글·영숫·_만, 30자)
    date_str: str = ""                      # YYMMDD
    tone: str = ""                          # 재미형, 공감형, 슬픔/감동형, 관찰형, 발견형, 정보형
    score: float = 0.0
    key_phrase: str = ""
    selection_reason: str = ""


@dataclass
class EditPlanShorts:
    start_s: float = 0.0
    end_s: float = 0.0
    duration_s: float = 0.0
    preserve_aspect: bool = True


@dataclass
class FocusBox:
    """클립별 zoom/crop 영역. 정규화 좌표 (0~1, 원본 영상 width/height 비율).

    v1.2 NEW. 코워크가 face_clusters / Vision 으로 화자·상황을 식별해
    1:1 영상 박스에 어떻게 채울지 지정. 정사각형이 아니어도 OK —
    로컬 렌더러가 1:1 박스에 채울 때 가운데 align + scale 처리.

    `reason`: 채택 사유 ("current_speaker" | "next_speaker" | "hide_caption" | ...).
    디버그·로깅 용도. 렌더링에는 영향 X.
    """
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    reason: str = ""


@dataclass
class HideCaptionRegion:
    """★ v1.10.8 NEW — 자체 자막 mask 영역 (검정 박스 덮기).

    정규화 좌표 (0~1, Top-Left origin). edit_plan_validator 가 sub_cut 시간대 OCR
    결과 중 *dialog substring 일치* 자체 자막의 bbox 자동 채움. LLM 박지 X.

    edit.py rendering 단계에서 sub_cut 영역에 검정 직사각형 draw — 원본 자체 자막
    가림. focus_box crop 과 별도 layer.
    """
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    text: str = ""                      # 디버그·로깅용 (어떤 자막 가리는지)


@dataclass
class EditPlanSubCut:
    index: int
    start: float
    end: float
    duration: float
    # v1.2 NEW — 클립별 zoom/crop. None 이면 폴백 (16:9 → 1:1 상단 crop).
    focus_box: Optional[FocusBox] = None
    # ★ v1.10.8 NEW — 자체 자막 mask 영역. edit_plan_validator 가 자동 채움
    hide_caption_regions: list[HideCaptionRegion] = field(default_factory=list)


@dataclass
class SubtitleStyle:
    """ASS 스타일.

    ASS \\an 정렬: 1~3 = 하단(왼/중/오), 4~6 = 중앙, 7~9 = 상단.
    primary_colour / outline_colour 는 ASS 16진 형식 (&HBBGGRR&).

    줄별 위치 변경은 SRT 텍스트에 ASS 인라인 태그 박는 식:
        {\\an8\\pos(640,400)}(타블로 진심)

    얼굴 추적 자막은 explain.srt 에 [follow:N] 마커:
        (투컷 당황) [follow:1 offset:y=-40]
    렌더러가 face_clusters.json 보고 \\move() waypoints 자동 생성.

    v1.2 — ASS PlayRes 가 캔버스(1080×1920) 기준으로 변경됨.
    자막은 영상이 아닌 캔버스 위에 burn — 좌표계도 캔버스 기준.
        - dialog 디폴트: 자막 영역(y≈1400~1840) 가운데
        - explain 디폴트: 영상 박스(y=320~1400) 가운데 위쪽
    """
    alignment: int = 2                      # 2=bottom-center, 8=top-center
    font_name: str = "Noto Sans CJK KR"
    font_size: int = 44
    bold: bool = True
    primary_colour: str = "&HFFFFFF&"
    outline_colour: str = "&H000000&"
    outline: int = 2
    margin_v: int = 80


def _default_dialog_style() -> SubtitleStyle:
    """대사 자막 — 자막 영역(★ v1.9.2 검정 배경) 안 흰 텍스트 + 검정 외곽선.

    v1.4.1 — 90pt 두 줄이 자막영역(440px) 침범 → 72pt + margin_v=200 으로 절충.
    실제 line_height 가 폰트의 ~1.6배라 90pt 두 줄 ≈ 290px (margin 포함 350px+) → 침범.
    72pt 두 줄 ≈ 230px → 자막영역 안에 안전.
    margin_v=200 → 자막을 자막영역 하단부로 (y≈1720, 영상 박스로 침범 X).

    ★ v1.9.2 — 자막 영역 흰 → 검정 배경 변경에 따라 텍스트 색감 흰글씨 + 검정 외곽선
    으로 통일. 캡컷 import 호환 + 자동렌더 단순화 + 한국 쇼츠 표준 가독성.
    """
    return SubtitleStyle(
        alignment=2,
        font_size=72,                       # v1.4.1 — 90pt 침범 fix
        primary_colour="&HFFFFFF&",         # ★ v1.9.2 — 검정 → 흰
        outline_colour="&H000000&",
        outline=6,                          # ★ v1.9.2 — 외곽선 0 → 6 (검정 배경 위 가독성)
        margin_v=200,                       # 1920 - 200 = 1720 (자막영역 하단부)
        bold=True,
    )


def _default_explain_style() -> SubtitleStyle:
    """행동/감정 자막 — 영상 박스 안 위쪽, 노란색, 90pt, 두꺼운 검정 외곽선.

    캔버스 PlayResY=1920 기준 alignment=8 + margin_v=420 → y≈420 (영상 박스 윗부분).
    """
    return SubtitleStyle(
        alignment=8,
        font_size=90,
        primary_colour="&H00FFFF&",         # 노란
        outline_colour="&H000000&",
        outline=6,                          # 외곽선 두껍게 (영상 위 가독성)
        margin_v=420,                       # 영상 박스(320~1400) 윗부분
        bold=True,
    )


@dataclass
class EditPlanSubtitles:
    dialog_srt_file: str = "dialog.srt"
    explain_srt_file: str = "explain.srt"
    dialog_style: SubtitleStyle = field(default_factory=_default_dialog_style)
    explain_style: SubtitleStyle = field(default_factory=_default_explain_style)


@dataclass
class EditPlanTitle:
    text: str
    format: str = ""                        # 설명형 / 비교형 / 수식어+명사 / 사건·조건형


@dataclass
class EditPlanEncoding:
    video_codec: str = "libx264"
    preset: str = "medium"                  # ultrafast | fast | medium | slow | veryslow
    crf: int = 18
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    pix_fmt: str = "yuv420p"


@dataclass
class EditPlanOutputs:
    folder_name: str = ""
    produce_full: bool = True
    produce_full_raw: bool = True
    produce_subcuts: bool = True


@dataclass
class EditPlanTemplate:
    """v1.1 NEW — PIL 템플릿 합성용.

    채널 정보 (mumakeshigh, 뮤맥하, 아이콘) 는 config.json 의 ChannelConfig 에서 가져옴.
    여기서는 영상별로 바뀌는 타이틀만 명세.
    """
    title_text: str = ""                    # 메인 타이틀 (개행 \n 포함 가능)
    title_line_count: int = 2


@dataclass
class EditPlanFooter:
    """v1.1 NEW — 출처 footer.

    source_meta.json.uploader 에서 자동 생성:
        "출처 - 유병재"
    """
    source_text: str = ""


@dataclass
class EditPlanMetadata:
    created_at: str = ""
    created_by: str = "cowork-claude"
    session_id: Optional[str] = None


def _default_metadata() -> EditPlanMetadata:
    return EditPlanMetadata(created_at=datetime.now().isoformat())


@dataclass
class EditPlan:
    """편집점/<날짜_제목>/edit_plan.json — 코워크 → 로컬 4단계 입력.

    이 한 파일이 4단계 자동 편집의 모든 명령서.
    schema_version 1.1 — template + footer 추가됨.
    """
    schema_version: str = SCHEMA_VERSION
    source: EditPlanSource = field(default_factory=EditPlanSource)
    candidate: EditPlanCandidate = field(default_factory=EditPlanCandidate)
    shorts: EditPlanShorts = field(default_factory=EditPlanShorts)
    sub_cuts: list[EditPlanSubCut] = field(default_factory=list)
    subtitles: EditPlanSubtitles = field(default_factory=EditPlanSubtitles)
    titles: list[EditPlanTitle] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    encoding: EditPlanEncoding = field(default_factory=EditPlanEncoding)
    outputs: EditPlanOutputs = field(default_factory=EditPlanOutputs)
    # v1.1 추가 — PIL 템플릿 합성
    template: EditPlanTemplate = field(default_factory=EditPlanTemplate)
    footer: EditPlanFooter = field(default_factory=EditPlanFooter)
    metadata: EditPlanMetadata = field(default_factory=_default_metadata)


# =============================================================================
# 7. (분석/편집/완성)/meta.json — 폴더 메타 (PRD §3.8)
# =============================================================================

@dataclass
class FolderMeta:
    """각 폴더의 요약 메타. 검색·중복 검사용."""
    schema_version: str = SCHEMA_VERSION
    kind: FolderMetaKind = FolderMetaKind.ANALYSIS_META
    title: str = ""
    date: str = ""                          # YYYY-MM-DD
    source_video_basename: str = ""
    candidate_id: Optional[int] = None
    duration_s: float = 0.0
    tone: Optional[str] = None
    score: Optional[float] = None


# =============================================================================
# 8. AppConfig — config.json (v1.1, PRD §11)
# =============================================================================

@dataclass
class ChannelConfig:
    """v1.1 NEW — 채널 정보. PIL 템플릿 합성에 사용. config.json 에 분리.

    여러 채널 운영 시 채널 프로필 갈아끼우는 식으로 확장 가능.
    """
    handle: str = ""                        # "mumakeshigh"
    name_kr: str = ""                       # "뮤맥하"
    icon_path: str = ""                     # "{data_root}/_assets/channel_icon.png"


@dataclass
class AppConfig:
    """~/Library/Application Support/showdon-yejja/config.json"""
    schema_version: str = SCHEMA_VERSION

    # 데이터 루트 (PRD §11) — 모든 절대 경로의 기준
    data_root: str = "~/showdon/yejjas"

    # 채널 정보 (v1.1)
    channel: ChannelConfig = field(default_factory=ChannelConfig)

    # STT 설정 — Apple Silicon GPU (MLX) 활용
    # ★ v1.9.3+ — faster-whisper 제거, mlx-whisper 단독 (검증 완료: 6.6x 빠름)
    stt_mlx_model_repo: str = "mlx-community/whisper-large-v3-mlx"
                                            # mlx-whisper 모델 path (Hugging Face)
    stt_language: Optional[str] = None     # None = auto-detect (한·영 혼용 권장). "ko"/"en" 강제 가능
    stt_temperature: float = 0.0           # 재현성 ↑. 0.0 = greedy
    stt_condition_on_previous: bool = False # cascade error 방지
    # v1.5 — 인물·고유명사 인식 정확도 ↑ (initial_prompt 로 도메인 어휘 사전 주입)
    stt_initial_prompt: str = (
        "에픽하이 타블로 투컷 미쿡이 다이나믹듀오 개코 최자 어반자카파 "
        "권순일 조현아 신동엽 유세윤 워크맨 정식이 이수만 은혁 동해 "
        "예원아빠 하루아빠 은우아빠 쇼츠 유튜브 아이돌."
    )

    # ★ v1.9.3+ — 초고성능 모드 제거. mlx-whisper + Apple Vision 모두 GPU only 라
    # CPU thread 조절 영향 거의 없음 (face_clusters 5초 단계만 미세 영향).
    # 단순화 위해 토글 / 필드 제거.

    # Scene·프레임
    scene_threshold: float = 0.3
    frame_extraction_fps: float = 1.0
    frame_scale_w: int = 1280              # Vision OCR 정확도 위해 720 → 1280 (PRD §2 어덴덤)

    # 로컬 OCR — ★ v1.9.3+ Apple Vision 단독 (PaddleOCR 제거됨)
    # macOS Vision framework, Metal GPU, ~10x 빠름. 한국어 정확도 우수.
    enable_local_ocr: bool = True          # 로컬 OCR 활성화
    local_ocr_min_score: float = 0.5       # 이 미만 신뢰도 텍스트는 무시

    # 큐 동작
    reanalysis_policy: ReanalysisPolicy = ReanalysisPolicy.ASK
    sleep_prevention: SleepPrevention = SleepPrevention.DURING_QUEUE

    # 인코딩 기본값 — M4 Pro 기준 medium 도 1~3분 쇼츠 1~2분에 끝남, 화질 더 좋음
    encoding_preset: str = "medium"
    encoding_crf: int = 18

    # 폰트 (자동 다운로드)
    font_path: str = "{data_root}/_fonts/NotoSansKR-Bold.otf"

    # v1.6 — CapCut Drafts 폴더 (사용자별 다를 수 있음)
    capcut_drafts_path: str = "~/Movies/CapCut/User Data/Projects/com.lveditor.draft"

    # v1.6 — VectCutAPI server URL (사용자가 별도 capcut_server.py 띄워야)
    capcut_server_url: str = "http://localhost:9000"
    # v1.6 — VectCutAPI server cwd (dfd_<id>/ 폴더 생성 위치). git clone 한 곳.
    capcut_server_cwd: str = "~/showdon/showdon-yejja/VectCutAPI"

    # ★ v1.9.3 — 디버그 자동출력 폴더 (자동편집 결과물 한 곳에 모아 비교용).
    # backend/edit.py 의 DEBUG_AUTO_OUTPUT_DIR 가 다음 우선순위로 결정:
    #   1) 환경변수 YEJJA_PROD=1 → None (PROD = 자동복사 OFF)
    #   2) 환경변수 YEJJA_DEBUG_AUTO_DIR (수동 override)
    #   3) AppConfig.debug_auto_output_dir (이 필드)
    #   4) 빈 문자열 또는 None = 자동복사 OFF
    # 디버깅 탭에서 GUI 로 변경 가능.
    debug_auto_output_dir: str = "~/showdon/yejjas_test/auto"


# =============================================================================
# 직렬화 유틸 — dict ↔ dataclass 재귀 변환
# =============================================================================

T = TypeVar("T")


def _convert(value: Any, type_hint: Any) -> Any:
    """dict → 타입 힌트 따라 변환 (재귀)."""
    if value is None:
        return None

    origin = get_origin(type_hint)

    # Optional[X] = Union[X, None]
    if origin is Union:
        args = [a for a in get_args(type_hint) if a is not type(None)]
        if len(args) == 1:
            return _convert(value, args[0])
        return value

    # list[X]
    if origin is list:
        item_type = get_args(type_hint)[0]
        return [_convert(v, item_type) for v in value]

    # dict[K, V]
    if origin is dict:
        args = get_args(type_hint)
        if len(args) == 2:
            _, val_type = args
            return {k: _convert(v, val_type) for k, v in value.items()}
        return value

    # Enum
    if isinstance(type_hint, type) and issubclass(type_hint, Enum):
        return type_hint(value)

    # Dataclass
    if is_dataclass(type_hint):
        return from_dict(type_hint, value)

    # Primitive — 그대로
    return value


def from_dict(cls: Type[T], data: Optional[dict]) -> T:
    """dict → dataclass 인스턴스. 누락 필드는 기본값 사용. 알려지지 않은 필드는 무시.

    데이터 손상 시 치명적 에러보다 "최선 노력" 로딩이 우선 (GUI 가 부분 정보로도 열림).
    엄격한 검증이 필요하면 validate_schema_version() 함께 사용.
    """
    if data is None:
        return cls()  # type: ignore[call-arg]

    if not is_dataclass(cls):
        return data  # type: ignore[return-value]

    type_hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}

    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _convert(data[f.name], type_hints[f.name])

    return cls(**kwargs)  # type: ignore[call-arg]


def to_dict(instance: Any) -> Any:
    """dataclass / Enum / list / dict → 일반 dict (JSON 직렬화 가능 형태)."""
    if isinstance(instance, Enum):
        return instance.value
    if is_dataclass(instance) and not isinstance(instance, type):
        return {f.name: to_dict(getattr(instance, f.name)) for f in fields(instance)}
    if isinstance(instance, list):
        return [to_dict(v) for v in instance]
    if isinstance(instance, dict):
        return {k: to_dict(v) for k, v in instance.items()}
    return instance


# =============================================================================
# JSON 파일 I/O
# =============================================================================

def load_json(path: Path | str) -> dict:
    """파일에서 JSON 로드 (UTF-8)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path | str, data: Any, indent: int = 2) -> None:
    """JSON 파일로 저장 (UTF-8, BOM 없음, indent=2, ensure_ascii=False).

    부모 디렉토리 자동 생성. 끝에 newline 추가.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
        f.write("\n")


def load_dataclass(cls: Type[T], path: Path | str) -> T:
    """JSON 파일 → dataclass 인스턴스."""
    return from_dict(cls, load_json(path))


def save_dataclass(instance: Any, path: Path | str, indent: int = 2) -> None:
    """dataclass → JSON 파일."""
    save_json(path, to_dict(instance), indent=indent)


# =============================================================================
# 스키마 버전 검증
# =============================================================================

class SchemaVersionError(ValueError):
    """schema_version 불일치 또는 누락."""


def validate_schema_version(
    data: dict,
    expected: str = SCHEMA_VERSION,
    *,
    strict: bool = False,
) -> None:
    """JSON 의 schema_version 검사.

    - strict=False (기본): major 만 비교 (forward-compatible). 1.0/1.1/1.x 모두 OK.
    - strict=True: 정확히 일치해야 함.

    Raises:
        SchemaVersionError: 불일치 또는 누락 시.
    """
    actual = data.get("schema_version")
    if actual is None:
        raise SchemaVersionError("schema_version 필드가 없습니다")

    if strict:
        if actual != expected:
            raise SchemaVersionError(
                f"schema_version 불일치 (strict): expected={expected}, actual={actual}"
            )
        return

    actual_major = str(actual).split(".")[0]
    expected_major = str(expected).split(".")[0]
    if actual_major != expected_major:
        raise SchemaVersionError(
            f"schema_version major 불일치: expected={expected_major}.x, actual={actual}"
        )


# =============================================================================
# 마커 파일 헬퍼 (PRD §2.1)
# =============================================================================

def write_marker(folder: Path | str, marker: str, payload: Optional[dict] = None) -> None:
    """폴더에 마커 파일 생성. payload 가 있으면 JSON 으로 저장."""
    f = Path(folder)
    f.mkdir(parents=True, exist_ok=True)
    p = f / marker
    if payload is not None:
        save_json(p, payload)
    else:
        p.touch()


def has_marker(folder: Path | str, marker: str) -> bool:
    return (Path(folder) / marker).exists()


def remove_marker(folder: Path | str, marker: str) -> bool:
    """있으면 삭제. 삭제 여부 반환."""
    p = Path(folder) / marker
    if p.exists():
        p.unlink()
        return True
    return False


def clear_status_markers(folder: Path | str) -> None:
    """모든 상태 마커 제거 (재분석 시 사용)."""
    for m in (MARKER_DONE, MARKER_FAILED, MARKER_PARTIAL):
        remove_marker(folder, m)


# =============================================================================
# Public API
# =============================================================================

__all__ = [
    # 상수
    "SCHEMA_VERSION",
    "MARKER_DONE", "MARKER_READY", "MARKER_FAILED", "MARKER_PARTIAL",
    # Enum
    "CandidateKind", "AnalysisStatus", "StepStatus", "FolderMetaKind",
    "ReanalysisPolicy", "SleepPrevention",
    # SourceMeta (다운로드)
    "SourceMeta",
    # AnalysisMeta (1단계)
    "VideoInfo", "AudioInfo", "AnalysisError", "AnalysisProgress", "AnalysisMeta",
    # STT
    "SttWord", "SttSegment", "SttJson",
    # Scene
    "SceneCutsJson",
    # OCR
    "OcrCandidate", "OcrCandidatesJson",
    "OcrLocalEntry", "OcrLocalJson",
    # Transcript source (★ v1.9.5)
    "TranscriptSourceLang", "TranscriptSourceJson",
    # Face
    "FaceBboxNorm", "FaceDetection", "FrameFaces", "FaceCluster", "FaceClustersJson",
    # EditPlan (3단계)
    "EditPlanSource", "EditPlanCandidate", "EditPlanShorts", "EditPlanSubCut",
    "FocusBox",
    "SubtitleStyle", "EditPlanSubtitles", "EditPlanTitle", "EditPlanEncoding",
    "EditPlanOutputs", "EditPlanTemplate", "EditPlanFooter", "EditPlanMetadata",
    "EditPlan",
    # Folder
    "FolderMeta",
    # Config
    "ChannelConfig", "AppConfig",
    # 유틸
    "from_dict", "to_dict",
    "load_json", "save_json", "load_dataclass", "save_dataclass",
    "validate_schema_version", "SchemaVersionError",
    "write_marker", "has_marker", "remove_marker", "clear_status_markers",
]


# =============================================================================
# 스모크 테스트 (python backend/schema.py 로 실행)
# =============================================================================

if __name__ == "__main__":
    import sys

    print(f"schema_version = {SCHEMA_VERSION}")

    # 1. EditPlan 기본 생성 → JSON dump → 다시 로드
    plan = EditPlan()
    plan.candidate.id = 2
    plan.candidate.title_for_dir = "AI가_다_그려주는_시대"
    plan.candidate.date_str = "260505"
    plan.candidate.tone = "재미형"
    plan.candidate.score = 8.5
    plan.shorts = EditPlanShorts(start_s=60.0, end_s=180.0, duration_s=120.0)
    plan.sub_cuts = [
        EditPlanSubCut(index=1, start=60.0, end=68.5, duration=8.5),
        EditPlanSubCut(index=2, start=68.5, end=75.0, duration=6.5),
    ]
    plan.titles = [
        EditPlanTitle(text="AI가 다 그려주는 시대의 인생 상담", format="설명형"),
    ]
    plan.hashtags = ["#에픽하이", "#투컷", "#AI", "#예능"]
    plan.template.title_text = "AI가 다 그려주는 시대의\n인생 상담"
    plan.footer.source_text = "출처 - 에픽하이"

    d = to_dict(plan)
    s = json.dumps(d, indent=2, ensure_ascii=False)
    print("\n=== EditPlan → JSON ===")
    print(s)

    # 2. JSON → EditPlan 복원
    plan2 = from_dict(EditPlan, json.loads(s))
    assert plan2.candidate.title_for_dir == "AI가_다_그려주는_시대"
    assert plan2.shorts.duration_s == 120.0
    assert len(plan2.sub_cuts) == 2
    assert plan2.template.title_text == "AI가 다 그려주는 시대의\n인생 상담"
    assert plan2.footer.source_text == "출처 - 에픽하이"
    print("\n[OK] EditPlan 왕복 (dataclass → JSON → dataclass) 검증 통과")

    # 3. SourceMeta 왕복
    src = SourceMeta(
        url="https://www.youtube.com/watch?v=...",
        platform="youtube",
        uploader="유병재",
        uploader_handle="@yubyungjae",
        title="n년째 고용 중인 투컷 친구 대행 알바",
        upload_date="2026-04-30",
        duration_s=45.0,
        downloaded_at="2026-05-05T19:30:00+09:00",
    )
    src2 = from_dict(SourceMeta, to_dict(src))
    assert src2.uploader == "유병재"
    print("[OK] SourceMeta 왕복 검증 통과")

    # 4. AnalysisMeta — Enum + nested + Optional 왕복
    am = AnalysisMeta()
    am.video_basename = "260423-youtube-test"
    am.video.width = 1920
    am.video.height = 1080
    am.audio.present = True
    am.audio.codec = "aac"
    am.analysis.status = AnalysisStatus.RUNNING
    am.analysis.steps["stt"] = {"status": "running", "elapsed_s": 120,
                                "model": "faster-whisper-large-v3"}
    am.source = src
    am2 = from_dict(AnalysisMeta, to_dict(am))
    assert am2.analysis.status == AnalysisStatus.RUNNING
    assert am2.audio.present is True
    assert am2.source.uploader == "유병재"
    assert am2.analysis.steps["stt"]["model"] == "faster-whisper-large-v3"
    print("[OK] AnalysisMeta 왕복 검증 통과 (Enum + Optional + Nested + Dict)")

    # 5. schema_version 검증
    try:
        validate_schema_version({"schema_version": "1.0"})
        validate_schema_version({"schema_version": "1.5"})  # major 같으면 OK
        validate_schema_version({"schema_version": SCHEMA_VERSION}, strict=True)
        print("[OK] schema_version forward-compatible 검증 통과")
    except SchemaVersionError as e:
        print(f"[ERR] {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validate_schema_version({"schema_version": "2.0"})
        print("[ERR] 2.0 이 통과하면 안 됨", file=sys.stderr)
        sys.exit(1)
    except SchemaVersionError:
        print("[OK] schema_version major 불일치 거부 검증 통과")

    try:
        validate_schema_version({})
        print("[ERR] 누락 필드가 통과하면 안 됨", file=sys.stderr)
        sys.exit(1)
    except SchemaVersionError:
        print("[OK] schema_version 누락 거부 검증 통과")

    print("\n=== 모든 스모크 테스트 통과 ===")
