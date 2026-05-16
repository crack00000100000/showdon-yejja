# -*- coding: utf-8 -*-
"""
backend/edit_plan_validator.py — edit_plan.json build-time validator (v1.10.2)

v1.10.0 audit (2026-05-13) 발견 결함을 build pipeline 에서 자동 검증.
SYSTEM_PROMPT v1.10.2 의 hard rule 들을 코드 차원에서 강제.

검증 항목 + 자동 fix:
1. title 14자+ 줄바꿈 (auto-fix — title_line_count = 2 강제 + \n 어절 경계 삽입)
2. sub_cut boundary 발화 한가운데 X (§6.1 v1.10.2)
3. 펀치 timing ∈ shorts 범위 (§13.5 10c — selection_reason heuristic)
4. 회상 클립 / cutaway 인서트 (§13.5 11d — face_clusters dominant cluster 확인)

호출:
  validate_edit_plan(edit_plan_dict, dialog_cues, source_dir=None) -> dict
  auto_fix_title(edit_plan_dict) -> bool (mutate)

반환:
  violations: list of (severity, rule, message)
  fixes_applied: list of (rule, before, after)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

# 자동 줄바꿈 트리거 길이 (큰따옴표 제거 후 자수)
TITLE_WRAP_THRESHOLD = 16  # v1.10.5 — 간결 훅 룰. 5~12자 target, 16자+ 만 두 줄


def _strip_quotes(s: str) -> str:
    """큰따옴표 제거."""
    return s.strip().strip('"').strip('"').strip('"')


def _wrap_title(text: str, threshold: int = TITLE_WRAP_THRESHOLD) -> tuple[str, int]:
    """14자+ 시 자연 어절 경계 (공백) 에 \\n 삽입. 큰따옴표는 유지.

    Returns: (wrapped_text, line_count)
    """
    inner = _strip_quotes(text)
    if len(inner) < threshold:
        return text, 1
    # 이미 \n 들어있으면 손대지 X
    if "\n" in text:
        return text, text.count("\n") + 1
    # 어절 경계 후보 (공백) 중 *중앙에 가장 가까운* 위치
    words = inner.split(" ")
    if len(words) < 2:
        # 한 어절 14자+ — 줄바꿈 못 함, 그대로 두고 line_count=1
        return text, 1
    # 어절 누적 길이로 중앙 분할
    total = sum(len(w) for w in words) + len(words) - 1  # 공백 포함
    half = total / 2
    acc = 0
    split_idx = 0
    for i, w in enumerate(words):
        acc += len(w) + (1 if i > 0 else 0)
        if acc >= half:
            split_idx = i + 1
            break
    line1 = " ".join(words[:split_idx])
    line2 = " ".join(words[split_idx:])
    if not line2:
        return text, 1
    wrapped_inner = f"{line1}\n{line2}"
    # 큰따옴표 복원
    if text.startswith('"') and text.endswith('"'):
        return f'"{wrapped_inner}"', 2
    return wrapped_inner, 2


def auto_fix_title(plan: dict[str, Any]) -> list[tuple[str, str, str]]:
    """template.title_text 14자+ 시 자동 줄바꿈 + line_count=2 강제.

    Returns: list of (rule, before, after) 적용된 fix
    """
    fixes = []
    template = plan.get("template", {})
    title_text = template.get("title_text", "")
    current_lc = template.get("title_line_count", 1)
    inner = _strip_quotes(title_text)
    if len(inner) < TITLE_WRAP_THRESHOLD:
        return fixes
    new_text, new_lc = _wrap_title(title_text)
    if new_text != title_text or new_lc != current_lc:
        template["title_text"] = new_text
        template["title_line_count"] = new_lc
        plan["template"] = template
        fixes.append((
            "title_14plus_wrap",
            f"{title_text!r} line_count={current_lc}",
            f"{new_text!r} line_count={new_lc}",
        ))
    return fixes


def _normalize_for_match(text: str) -> str:
    """공백 / 부호 / 검열 기호 제거 — substring 비교 normalize."""
    return re.sub(r"[\s@\W]+", "", text)


def _bbox_union(a: dict, b: dict) -> dict:
    """두 bbox 의 합집합 (left·top min, right·bottom max)."""
    ax2 = a["x"] + a["w"]
    ay2 = a["y"] + a["h"]
    bx2 = b["x"] + b["w"]
    by2 = b["y"] + b["h"]
    nx = min(a["x"], b["x"])
    ny = min(a["y"], b["y"])
    nx2 = max(ax2, bx2)
    ny2 = max(ay2, by2)
    return {"x": nx, "y": ny, "w": nx2 - nx, "h": ny2 - ny}


def auto_fill_hide_caption_regions(
    plan: dict,
    dialog_cues: list,
    ocr_local: Optional[dict],
) -> list[tuple[str, str, str]]:
    """★ v1.10.8 — sub_cut 시간대 OCR regions 중 dialog substring 일치 자막의 bbox 자동 채움.

    sub_cut.hide_caption_regions 에 mutate. 기존 OCR 영상 (regions=None) 은 graceful skip.

    Args:
        plan: edit_plan dict
        dialog_cues: explain_validator.Cue 리스트 (sub_cut 안 timeline 기준)
        ocr_local: ocr_local.json dict 또는 None

    Returns: 적용된 fix list — (rule, before, after)
    """
    fixes = []
    if not ocr_local or "entries" not in ocr_local:
        return fixes
    entries = ocr_local.get("entries", [])
    sub_cuts = plan.get("sub_cuts", [])
    if not sub_cuts:
        return fixes

    # 전체 dialog 텍스트 정규화 (sub_cut 단위로 매칭하려면 시각 변환 필요)
    # dialog cue 의 timeline 은 sub_cut 안 누적 — 절대 시각 매핑 필요
    # 간단하게: 모든 dialog 텍스트를 한 string 으로 normalize
    dialog_full = _normalize_for_match(" ".join(c.text for c in dialog_cues))
    if not dialog_full:
        return fixes

    for sc in sub_cuts:
        sc_start = sc.get("start", 0)
        sc_end = sc.get("end", 0)
        # 이 sub_cut 시간대의 OCR entries
        sc_entries = [e for e in entries if sc_start <= e.get("t_abs", -1) <= sc_end]
        # text별 bbox 합집합
        text_to_bbox: dict[str, dict] = {}
        for e in sc_entries:
            regions = e.get("regions")
            if not regions:
                continue
            for r in regions:
                text = r.get("text", "").strip()
                if not text or len(text) < 2:
                    continue
                norm = _normalize_for_match(text)
                if not norm or norm not in dialog_full:
                    # dialog 와 일치 X — 워터마크 / 코너자막 / 무관 텍스트
                    continue
                bbox = {
                    "x": r.get("x", 0), "y": r.get("y", 0),
                    "w": r.get("w", 0), "h": r.get("h", 0),
                }
                if text in text_to_bbox:
                    text_to_bbox[text] = _bbox_union(text_to_bbox[text], bbox)
                else:
                    text_to_bbox[text] = bbox
        # mutate sub_cut.hide_caption_regions
        if text_to_bbox:
            new_regions = [
                {"x": round(b["x"], 4), "y": round(b["y"], 4),
                 "w": round(b["w"], 4), "h": round(b["h"], 4), "text": t}
                for t, b in text_to_bbox.items()
            ]
            sc["hide_caption_regions"] = new_regions
            fixes.append((
                "hide_caption_auto_fill",
                f"sub_cut[{sc.get('index')}] regions=0",
                f"sub_cut[{sc.get('index')}] regions={len(new_regions)} (texts: {list(text_to_bbox.keys())[:3]})",
            ))
    return fixes


def check_single_cut_long_duration(
    sub_cuts: list[dict],
    shorts: dict,
    min_long_s: float = 60.0,
) -> list[tuple[str, str, str]]:
    """v1.10.7 (D) — single sub_cut 인데 duration ≥ 60s 인 케이스 hard reject.

    audit 발견 (v1.10.5 176 영상): single sub_cut + 60s+ 영상 8개 → 거의 wide
    shot 단체샷 retention 약점. 컷 분할 강제.
    """
    violations = []
    duration = shorts.get("duration_s") or (shorts.get("end_s", 0) - shorts.get("start_s", 0))
    if len(sub_cuts) == 1 and duration >= min_long_s:
        violations.append((
            "hard", "6.1_single_cut_60s",
            f"single sub_cut + duration {duration:.1f}s ≥ {min_long_s:.0f}s — "
            f"wide shot 단체샷 retention 약점 패턴. sub_cut 분할 의무 "
            f"(scene_cut / 화자 transition / 펀치라인 직전 등 §6.1 trigger)"
        ))
    return violations


def check_sub_cut_mid_speech(
    sub_cuts: list[dict],
    dialog_cues: list,  # list of Cue
    tolerance_s: float = 0.05,
) -> list[tuple[str, str, str]]:
    """§6.1 v1.10.2 — sub_cut 경계가 dialog cue 한가운데 자르는지.

    dialog_cues: explain_validator.Cue 호환 (start, end, text)
    """
    violations = []
    boundaries = set()
    for c in sub_cuts:
        boundaries.add(round(c["start"], 3))
        boundaries.add(round(c["end"], 3))
    for b in boundaries:
        for dc in dialog_cues:
            # source 절대 시각 기준 (sub_cut 도 절대 시각, dialog 는 sub_cut 안 timeline 이 아님 — 두 시각계가 다르면 변환 필요)
            # 그러나 dialog.srt 가 sub_cut timeline 기준이라면 비교 X.
            # source 시각 기준 dialog 가 들어와야 정확. heuristic: dialog 큐 길이가 1초+ 면 검증
            if dc.end - dc.start < 1.0:
                continue
            if dc.start + tolerance_s < b < dc.end - tolerance_s:
                violations.append((
                    "soft", "6.1_mid_speech",
                    f"sub_cut boundary {b:.2f}s 가 dialog cue "
                    f"({dc.start:.2f}~{dc.end:.2f}s) 한가운데 자름. silence point 로 snap 검토"
                ))
                break
    return violations


def check_punch_in_shorts(
    candidate: dict,
    shorts: dict,
) -> list[tuple[str, str, str]]:
    """§13.5 10c — selection_reason 의 펀치 시점이 shorts 범위 안인지 (heuristic).

    selection_reason 본문에 "펀치 X초" 같은 패턴 있으면 검증.
    """
    violations = []
    reason = candidate.get("selection_reason", "")
    shorts_start = shorts.get("start_s", 0)
    shorts_end = shorts.get("end_s", 0)
    # heuristic regex: "펀치 N초", "punch N s", "N초 ... 펀치"
    # 보통 selection_reason 은 상대 시각 (셋업 6s + 빌드업 6s + 펀치 5s ...) 형식이라
    # 절대 시각 검증은 어려움. 대신 "여운 0s" 또는 "punch ... outside" 패턴만 잡음.
    if re.search(r"여운\s*0\s*s|여운\s*없", reason):
        violations.append((
            "soft", "13.5_10c_no_outro",
            f"selection_reason 에 여운 0s — 펀치 후 여운 부족. shorts.end_s 연장 검토"
        ))
    # selection_reason 안 "punch_outside" 명시 (LLM 이 self-report 할 수 있음)
    if "펀치 클립 밖" in reason or "punch outside" in reason.lower():
        violations.append((
            "hard", "13.5_10c_punch_outside",
            f"selection_reason 에 펀치 클립 밖 명시 — shorts.end_s 연장 필수"
        ))
    return violations


# ★ v1.10.7 — check_recall_clip 폐기.
# 사유: 176 영상 audit 결과 120/176 (68%) false positive. face_clusters.json 의
# cluster_id 가 fine-grained 라 같은 인물도 매 frame cluster_id 다름 → cluster
# 전환 = 회상 클립 가정 X. v1.10.9 의 cluster consolidation + cast_list 매핑
# 인프라 fix 후 재활성화 검토.


def _load_ocr_local(source_dir: Optional[Path]) -> Optional[dict]:
    """source_dir 안 ocr_local.json 로드 (없으면 None)."""
    if not source_dir:
        return None
    p = Path(source_dir) / "ocr_local.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def validate_edit_plan(
    plan: dict[str, Any],
    dialog_cues: list,  # list of explain_validator.Cue
    source_dir: Optional[Path] = None,
    auto_fix: bool = True,
    ocr_local: Optional[dict] = None,
) -> dict[str, Any]:
    """edit_plan.json 전체 검증 + auto-fix.

    Args:
      plan: edit_plan.json 의 dict
      dialog_cues: explain_validator.parse_srt(dialog.srt) 결과
      source_dir: analysis 폴더 (face_clusters / cast_list / ocr_local 참조용)
      auto_fix: True 면 title 자동 줄바꿈 + hide_caption_regions 자동 채움 (plan mutate)
      ocr_local: 미리 로드된 ocr_local.json (None 이면 source_dir 에서 자동 로드 시도)
    """
    violations: list[tuple[str, str, str]] = []
    fixes_applied: list[tuple[str, str, str]] = []

    if auto_fix:
        fixes_applied.extend(auto_fix_title(plan))
        # v1.10.8 — hide_caption_regions 자동 채움 (OCR bbox 기반)
        if ocr_local is None:
            ocr_local = _load_ocr_local(source_dir)
        fixes_applied.extend(auto_fill_hide_caption_regions(plan, dialog_cues, ocr_local))

    sub_cuts = plan.get("sub_cuts", [])
    violations.extend(check_sub_cut_mid_speech(sub_cuts, dialog_cues))

    candidate = plan.get("candidate", {})
    shorts = plan.get("shorts", {})
    violations.extend(check_punch_in_shorts(candidate, shorts))
    violations.extend(check_single_cut_long_duration(sub_cuts, shorts))

    # v1.10.7 — recall_clip 룰 폐기 (위 주석 참조)

    return {
        "violations": violations,
        "fixes_applied": fixes_applied,
        "stats": {
            "n_sub_cuts": len(sub_cuts),
            "title_line_count": plan.get("template", {}).get("title_line_count", 1),
            "hard_violations": sum(1 for v in violations if v[0] == "hard"),
            "soft_violations": sum(1 for v in violations if v[0] == "soft"),
            "hide_caption_filled": sum(1 for f in fixes_applied if f[0] == "hide_caption_auto_fill"),
        },
    }


if __name__ == "__main__":
    # CLI 단발 사용
    import sys
    from backend.explain_validator import parse_srt as _parse_srt

    if len(sys.argv) < 2:
        print("usage: python -m backend.edit_plan_validator <편집점 폴더>")
        sys.exit(1)
    folder = Path(sys.argv[1])
    plan_path = folder / "edit_plan.json"
    dialog_path = folder / "dialog.srt"
    source_dir = folder.parent.parent / "분석"
    if not source_dir.exists():
        source_dir = None
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    dialog_cues = _parse_srt(dialog_path)
    result = validate_edit_plan(plan, dialog_cues, source_dir, auto_fix=False)
    print(f"=== {folder.name} ===")
    print(f"stats: {result['stats']}")
    for rule, before, after in result["fixes_applied"]:
        print(f"  [fix] {rule}: {before} → {after}")
    for sev, rule, msg in result["violations"]:
        print(f"  [{sev}] {rule} — {msg}")
