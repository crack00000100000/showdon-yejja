# -*- coding: utf-8 -*-
"""
test_capcut_template.py — pyCapCut 의 duplicate_as_template 검증.

macOS CapCut 의 정상 draft (0507) 를 template 으로 복제 → 비디오 + 자막 추가 →
새 draft 폴더 구조 확인. CapCut 에서 열리는지 사용자가 확인.

실행:
    cd ~/showdon/showdon-yejja
    source venv/bin/activate
    pip install pycapcut    # 아직이면
    python scripts/test_capcut_template.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

DRAFTS_PATH = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
TEMPLATE_NAME = "0507"             # 빈 macOS template
NEW_DRAFT_NAME = "_pycapcut_test"   # 검증용 새 draft 이름 (앞에 _ — 구분)


def main() -> int:
    if not DRAFTS_PATH.exists():
        print(f"❌ CapCut Drafts 폴더 없음: {DRAFTS_PATH}")
        return 1

    template_path = DRAFTS_PATH / TEMPLATE_NAME
    if not template_path.exists():
        print(f"❌ Template draft 없음: {template_path}")
        print(f"   먼저 CapCut 에서 '{TEMPLATE_NAME}' draft 만들어두세요.")
        return 1

    new_path = DRAFTS_PATH / NEW_DRAFT_NAME
    if new_path.exists():
        print(f"기존 '{NEW_DRAFT_NAME}' draft 있음 — 삭제하고 재생성")
        shutil.rmtree(new_path)

    print(f"=== Step 1: pycapcut import ===")
    try:
        import pycapcut as cc
    except ImportError as e:
        print(f"❌ pycapcut 미설치 — venv 에서 'pip install pycapcut'")
        print(f"   ({e})")
        return 1
    print(f"✅ pycapcut version: {getattr(cc, '__version__', '?')}")

    print(f"\n=== Step 2: DraftFolder 생성 ===")
    draft_folder = cc.DraftFolder(str(DRAFTS_PATH))
    print(f"✅ DraftFolder({DRAFTS_PATH})")

    print(f"\n=== Step 3: '{TEMPLATE_NAME}' template 복제 → '{NEW_DRAFT_NAME}' ===")
    try:
        script = draft_folder.duplicate_as_template(TEMPLATE_NAME, NEW_DRAFT_NAME)
        print(f"✅ duplicate_as_template 성공")
    except Exception as e:
        print(f"❌ duplicate_as_template 실패: {type(e).__name__}: {e}")
        print(f"   → pycapcut 이 macOS template (draft_info.json) 인식 못 함")
        print(f"   → 옵션 C 회귀 (단순 폴더 export) 진행 필요")
        return 1

    print(f"\n=== Step 4: 새 draft 폴더 구조 확인 ===")
    if new_path.exists():
        files = sorted(new_path.iterdir())
        print(f"   {len(files)} 개 항목:")
        for f in files[:20]:
            kind = "📁" if f.is_dir() else "📄"
            size = f.stat().st_size if f.is_file() else "-"
            print(f"   {kind} {f.name}  ({size}b)")
        if len(files) > 20:
            print(f"   ... +{len(files)-20}개 더")
    else:
        print(f"❌ 새 draft 폴더 안 생김: {new_path}")
        return 1

    # draft_info.json vs draft_content.json 어느 게 있는지 핵심
    has_info = (new_path / "draft_info.json").exists()
    has_content = (new_path / "draft_content.json").exists()
    print(f"\n   draft_info.json    (macOS format): {'✅' if has_info else '❌'}")
    print(f"   draft_content.json (Win format) : {'✅' if has_content else '❌'}")

    print(f"\n=== Step 5: 저장 (script.save) ===")
    try:
        script.save()
        print(f"✅ save 성공")
    except Exception as e:
        print(f"❌ save 실패: {type(e).__name__}: {e}")
        return 1

    print(f"\n=== Step 6: 저장 후 폴더 재확인 ===")
    files_after = sorted(new_path.iterdir())
    print(f"   {len(files_after)} 개 항목 (변경 전 {len(files)})")
    has_info2 = (new_path / "draft_info.json").exists()
    has_content2 = (new_path / "draft_content.json").exists()
    print(f"   draft_info.json    : {'✅' if has_info2 else '❌'}")
    print(f"   draft_content.json : {'✅' if has_content2 else '❌'}")

    print(f"\n" + "=" * 60)
    print(f"검증 완료. 다음 단계:")
    print(f"  1. CapCut 앱 열기 (이미 켜져있으면 새로고침/재시작)")
    print(f"  2. draft 목록에 '{NEW_DRAFT_NAME}' 보이는지 확인")
    print(f"  3. 클릭해서 열림? 콘텐츠 보임? 편집 가능?")
    print(f"  4. 결과 알려주세요.")
    print(f"=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
