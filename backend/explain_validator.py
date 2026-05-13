# -*- coding: utf-8 -*-
"""
backend/explain_validator.py — explain.srt build-time validator (v1.10.2)

v1.10.0 audit (2026-05-13) 발견 결함을 build pipeline 에서 자동 검증.
SYSTEM_PROMPT v1.10.2 의 hard rule 들 (LLM 산출물이 통과한 룰) 을 코드 차원에서 강제.

검증 항목:
1. explain.start = dialog.start ±200ms snap (§9.10.1)
2. cue duration 정수 round 패턴 감지 (§9.10.1)
3. 편집·제작 메모성 narration (§9.1 — 인서트 따주세요 등)
4. `(?)` `(?????)` 컨텍스트 검증 (§9.10.3 — cue 시점 ±2초 dialog 의문/놀람 여부)
5. 인물명 환각 grep (§9.5 — cast_list / dialog / OCR / source_meta 에 등장 여부)

호출: validate_explain(explain_path, dialog_path, source_dir=None) -> dict
  - source_dir: analysis 폴더 경로 (cast_list / OCR / source_meta 참조용, optional)

반환: dict
  - violations: list of (severity, rule, cue_index, cue_time, message)
    severity ∈ {"hard", "soft", "info"}
    hard = LLM 재요청 권장 / soft = 경고 / info = 통계
  - stats: dict of {n_cues, avg_dur, snap_pass_rate, ...}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# 편집·제작 메모성 패턴 — §9.1 (1) hard rule
_EDITOR_MEMO_PATTERNS = [
    r"인서트\s*따",          # (인서트 따주세요)
    r"이\s*컷\s*살려",       # (이 컷 살려주세요)
    r"다음\s*장면",          # (다음 장면에..)
    r"편집\s*해\s*주",       # (편집해주세요)
    r"붙여\s*넣",            # (붙여넣어주세요)
    r"빼\s*주",              # (빼주세요)
    r"잘라\s*주",            # (잘라주세요)
]

# 의문 motif 패턴 — §9.10.3 컨텍스트 검증 대상
_QUESTION_MOTIF_PATTERN = re.compile(r"^\(\s*\?+\s*\)$")

# dialog 본문에서 *의문/놀람/멈춤* 컨텍스트 마커
_QUESTION_CONTEXT_PATTERNS = [
    r"\?",                   # 모든 ? 종결
    r"~+\?",                 # ~?, ~~?
    r"엉\?",
    r"어\?+",
    r"네\?+",
    r"뭐\??",
    r"누구",
    r"어디",
    r"어떻게",
    r"왜",
    r"진짜\?",
    r"갑자기",
    r"잠깐\s*만",
    r"\.{3,}",               # ... (멈춤)
    r"~{2,}\s*$",            # 끝의 ~~ (말 흐림)
    r"\(흠\)",
    r"\(\?+\)",              # 다른 cue 의 ? 도 컨텍스트 마커로 인정
]
_QUESTION_CONTEXT_REGEX = re.compile("|".join(_QUESTION_CONTEXT_PATTERNS))

# 인물명 환각 grep — 호칭 접미 (언니/오빠/형/누나/씨/님/이/가) 가 *뒤에* 붙은 한국어 단어만
# false positive 줄이기 — 일반 명사 ("결론", "도전", "셋업") 가 단독으론 grep 대상 X.
# v1.10.0 audit 의 "다영 언니" 패턴: 호칭 접미 매칭으로 정확히 잡음.
_HONORIFIC_PATTERN = re.compile(
    r"([가-힣]{2,4})\s*(?:언니|오빠|형|누나|선배|선생님|님|씨|이|가)(?=\s|$|[ㄱ-ㅎ가-힣]+(?:\s|$))"
)


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str


def parse_srt(srt_path: Path) -> list[Cue]:
    """SRT 파일 파싱 — index/start/end/text."""
    if not srt_path.exists():
        return []
    content = srt_path.read_text(encoding="utf-8")
    cues: list[Cue] = []
    for block in re.split(r"\n\n+", content.strip()):
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", lines[1]
        )
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        try:
            idx = int(lines[0])
        except ValueError:
            idx = len(cues) + 1
        text = "\n".join(lines[2:])
        cues.append(Cue(index=idx, start=s, end=e, text=text))
    return cues


def _strip_text(t: str) -> str:
    """공백/검열/부호 제거 — 한국어 문자만 비교."""
    t = re.sub(r"[@\s]", "", t)
    t = re.sub(r"[^\w가-힣]", "", t)
    return t


def check_start_snap(
    explain_cues: list[Cue],
    dialog_cues: list[Cue],
    tolerance_s: float = 0.2,
) -> list[tuple[str, str, int, float, str]]:
    """§9.10.1 — 모든 explain cue 의 start 가 dialog cue start 와 ±200ms align."""
    violations = []
    if not dialog_cues:
        return violations
    dialog_starts = [c.start for c in dialog_cues]
    for ex in explain_cues:
        nearest = min(dialog_starts, key=lambda v: abs(v - ex.start))
        gap = abs(ex.start - nearest)
        if gap > tolerance_s:
            violations.append((
                "hard", "9.10.1_snap",
                ex.index, ex.start,
                f"explain start {ex.start:.2f}s 가 dialog start 와 {gap*1000:.0f}ms 어긋남 "
                f"(가장 가까운 dialog start = {nearest:.2f}s). ±{tolerance_s*1000:.0f}ms snap 필요"
            ))
    return violations


def check_duration_round_pattern(
    explain_cues: list[Cue],
) -> list[tuple[str, str, int, float, str]]:
    """§9.10.1 — cue duration 이 정수 round 패턴 (모든 cue 가 2.5/3.0/4.0 같은 round) 인지 감지."""
    violations = []
    durations = [c.end - c.start for c in explain_cues]
    if len(durations) < 3:
        return violations
    # 모든 duration 이 0.5초 단위 round 인지 (즉 dur * 2 가 정수에 ±0.05 안)
    round_count = sum(1 for d in durations if abs(d * 2 - round(d * 2)) < 0.05)
    ratio = round_count / len(durations)
    if ratio > 0.9 and len(set(round(d, 2) for d in durations)) <= 3:
        violations.append((
            "soft", "9.10.1_duration_round",
            -1, 0.0,
            f"explain cue duration {ratio*100:.0f}% 가 0.5초 round 패턴 "
            f"({round_count}/{len(durations)}). dialog 발화 길이 + 0.5초 fade 로 동적 권장"
        ))
    return violations


def check_editor_memo(
    explain_cues: list[Cue],
) -> list[tuple[str, str, int, float, str]]:
    """§9.1 (1) hard rule — 편집·제작 메모성 narration 발견."""
    violations = []
    for ex in explain_cues:
        for pat in _EDITOR_MEMO_PATTERNS:
            if re.search(pat, ex.text):
                violations.append((
                    "hard", "9.1_editor_memo",
                    ex.index, ex.start,
                    f"편집·제작 메모성 patterns 감지: {ex.text.strip()!r}. "
                    f"그 시점 frame 상황 narration 으로 교체"
                ))
                break
    return violations


def check_question_mark_context(
    explain_cues: list[Cue],
    dialog_cues: list[Cue],
    context_window_s: float = 2.0,
) -> list[tuple[str, str, int, float, str]]:
    """§9.10.3 — `(?)` `(?????)` 의문 motif 의 컨텍스트 정합 검증."""
    violations = []
    for ex in explain_cues:
        if not _QUESTION_MOTIF_PATTERN.match(ex.text.strip()):
            continue
        # cue 시점 ±context_window_s 안 dialog 본문에 의문 컨텍스트 마커가 있는지
        ctx_dialogs = [
            c for c in dialog_cues
            if ex.start - context_window_s < c.start < ex.end + context_window_s
        ]
        ctx_text = " ".join(c.text for c in ctx_dialogs)
        if not _QUESTION_CONTEXT_REGEX.search(ctx_text):
            violations.append((
                "soft", "9.10.3_question_context",
                ex.index, ex.start,
                f"의문 motif {ex.text.strip()!r} 시점 ±{context_window_s}s dialog 에 "
                f"의문/놀람/멈춤 마커 없음. 칭찬·감탄·폭소 자리일 수 있음 → Family 4/12 교체 검토. "
                f"컨텍스트: {ctx_text[:80]!r}"
            ))
    return violations


def _extract_name_candidates(text: str) -> list[str]:
    """explain 텍스트에서 인물명 후보 추출 — 호칭 접미가 뒤에 붙은 한글 단어만.

    v1.10.0 audit 의 환각 케이스 ("다영 언니", "혜정님") 같이 호칭 접미가 뒤따르는
    한글 2~4자 단어만 grep 대상. 일반 명사 단독 등장 ("결론", "도전") false positive 회피.
    """
    cleaned = re.sub(r"[()@\d]", " ", text)
    # 호칭 접미 매칭 — 다영 언니 / 혜정님 / 종운 씨 같은 패턴
    candidates: list[str] = []
    # 명시적 호칭 접미 패턴
    for honor in ["언니", "오빠", "형", "누나", "선배", "선생님", "님", "씨"]:
        for m in re.finditer(rf"([가-힣]{{2,4}})\s*{honor}", cleaned):
            candidates.append(m.group(1))
    return candidates


def check_person_name_hallucination(
    explain_cues: list[Cue],
    dialog_cues: list[Cue],
    source_dir: Optional[Path] = None,
) -> list[tuple[str, str, int, float, str]]:
    """§9.5 v1.10.2 — explain 안 인물명이 cast_list / dialog / OCR / source_meta 에 등장하는지 grep."""
    violations = []
    # ground truth 풀: dialog 본문 + OCR 본문 + source description + cast_list
    truth_text = " ".join(c.text for c in dialog_cues)
    cast_names: set[str] = set()
    if source_dir is not None and source_dir.exists():
        cast_path = source_dir / "cast_list.json"
        if cast_path.exists():
            try:
                cast_data = json.loads(cast_path.read_text(encoding="utf-8"))
                for entry in cast_data.get("clusters", []) if isinstance(cast_data, dict) else cast_data:
                    if isinstance(entry, dict) and entry.get("name"):
                        cast_names.add(entry["name"])
                        if entry.get("aliases"):
                            cast_names.update(entry["aliases"])
            except (json.JSONDecodeError, OSError):
                pass
        # face_clusters.json 의 label 도 fallback
        fc_path = source_dir / "face_clusters.json"
        if fc_path.exists():
            try:
                fc = json.loads(fc_path.read_text(encoding="utf-8"))
                for cl in fc.get("clusters", []):
                    if cl.get("label"):
                        cast_names.add(cl["label"])
            except (json.JSONDecodeError, OSError):
                pass
        # source_meta.json
        sm_path = source_dir.parent / "원본" / "source_meta.json"
        if sm_path.exists():
            try:
                sm = json.loads(sm_path.read_text(encoding="utf-8"))
                truth_text += " " + str(sm.get("uploader", "")) + " " + str(sm.get("description", "")) + " " + str(sm.get("title", ""))
            except (json.JSONDecodeError, OSError):
                pass
        # OCR
        for ocr_name in ("ocr_local.json", "ocr_candidates.json"):
            ocr_path = source_dir / ocr_name
            if ocr_path.exists():
                try:
                    ocr = json.loads(ocr_path.read_text(encoding="utf-8"))
                    truth_text += " " + json.dumps(ocr, ensure_ascii=False)
                except (json.JSONDecodeError, OSError):
                    pass

    for ex in explain_cues:
        for name in _extract_name_candidates(ex.text):
            in_dialog = name in truth_text
            in_cast = name in cast_names
            if not (in_dialog or in_cast):
                violations.append((
                    "soft", "9.5_name_hallucination",
                    ex.index, ex.start,
                    f"인물명 후보 {name!r} 이 dialog/cast_list/OCR/source_meta 어디에도 없음. "
                    f"환각 의심 → fallback (언니/옆 사람/게스트). cue: {ex.text.strip()!r}"
                ))
    return violations


def validate_explain(
    explain_path: Path,
    dialog_path: Path,
    source_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """explain.srt 전체 검증."""
    explain_cues = parse_srt(explain_path)
    dialog_cues = parse_srt(dialog_path)

    violations: list[tuple[str, str, int, float, str]] = []
    violations.extend(check_start_snap(explain_cues, dialog_cues))
    violations.extend(check_duration_round_pattern(explain_cues))
    violations.extend(check_editor_memo(explain_cues))
    violations.extend(check_question_mark_context(explain_cues, dialog_cues))
    violations.extend(check_person_name_hallucination(explain_cues, dialog_cues, source_dir))

    durations = [c.end - c.start for c in explain_cues]
    stats = {
        "n_cues": len(explain_cues),
        "avg_dur": round(sum(durations) / len(durations), 2) if durations else 0.0,
        "hard_violations": sum(1 for v in violations if v[0] == "hard"),
        "soft_violations": sum(1 for v in violations if v[0] == "soft"),
    }
    return {"violations": violations, "stats": stats}


if __name__ == "__main__":
    # CLI 단발 사용: python -m backend.explain_validator <편집점 폴더>
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m backend.explain_validator <편집점 폴더>")
        sys.exit(1)
    folder = Path(sys.argv[1])
    explain = folder / "explain.srt"
    dialog = folder / "dialog.srt"
    # source_dir = analysis 폴더 (편집점 부모/분석)
    source_dir = folder.parent.parent / "분석"
    if not source_dir.exists():
        source_dir = None
    result = validate_explain(explain, dialog, source_dir)
    print(f"=== {folder.name} ===")
    print(f"stats: {result['stats']}")
    for sev, rule, idx, t, msg in result["violations"]:
        print(f"  [{sev}] {rule} cue#{idx} @{t:.2f}s — {msg}")
