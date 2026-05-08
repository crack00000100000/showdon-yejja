# 쇼돈 예짜

뮤맥하 (음악·예능·하이라이트) 짜집기 쇼츠를 자동으로 만드는 macOS 앱입니다.
원본 토크 영상을 받아 → 분석 → 코워크 (Claude Cowork) 가 편집점을 짜고 → 캡컷 Draft 로 export 까지.

총 4 단계 흐름:

```
[다운로드]  →  [분석]  →  [편집점 작성 (코워크)]  →  [자동편집 (캡컷 또는 자동렌더)]
```

코워크가 STT / OCR / 얼굴 데이터를 읽고 sub_cut · 자막 · 제목 · 해시태그를 모두 작성합니다. 사용자는 다운로드 / 분석 / 편집점 요청 / 캡컷 export 만 클릭.

---

## 1. 터미널 열기

`Cmd + Space` → "터미널" 입력 → Enter

## 2. 아래 한 줄 복사해서 붙여넣고 Enter

```
cd ~/Downloads && rm -f 쇼돈_예짜_install.command && curl -fsSL -o 쇼돈_예짜_install.command "https://raw.githubusercontent.com/crack00000100000/showdon-yejja/main/%EC%87%BC%EB%8F%88_%EC%98%88%EC%A7%9C_install.command" && chmod +x 쇼돈_예짜_install.command && xattr -d com.apple.quarantine 쇼돈_예짜_install.command 2>/dev/null; bash 쇼돈_예짜_install.command
```

> **자동 실행 안 되면** (macOS 환경에 따라 가끔): Finder 에서 `~/Downloads/` 로 이동 → `쇼돈_예짜_install.command` **더블클릭**. (첫 실행 시 macOS 가 "확인되지 않은 개발자" 경고를 띄우면 → 우클릭 → **열기** → 다이얼로그에서 다시 **열기**)

## 3. 설치 진행

새 검은 창이 뜨면서 설치 자동 시작. 10~20 분 소요 (faster-whisper · mediapipe · PySide6 등 큰 패키지 포함).

도중에 두 번 액션 필요할 수 있음:
- Apple 다이얼로그가 뜨면 → "설치" 클릭
- `Password:` 가 뜨면 → Mac 로그인 비밀번호 입력 (화면에 안 찍혀도 정상) → Enter

## 4. 완료

`🎉 설치 모두 완료!` 메시지 뜨면 끝.

## 실행

`~/showdon/showdon-yejja/dist/쇼돈 예짜.app` 더블클릭.

처음에는 macOS 가 "확인되지 않은 개발자" 경고를 띄울 수 있어요 — 그땐 우클릭 → **열기** → 다이얼로그에서 다시 **열기** 클릭. 한 번만 허용하면 다음부턴 더블클릭으로 됩니다.

`.app` 을 **Dock에 끌어다 놓으면** 한 번 클릭으로 실행되고, `~/Applications/` 으로 옮기면 Spotlight 에서도 검색됩니다.

---

# 코워크 (Claude Cowork) 셋업 — 처음 한 번만

쇼돈 예짜는 **편집점 작성을 코워크 (Anthropic Claude desktop 의 Cowork mode) 에게 맡기는 구조**입니다. 그래서 처음에 코워크 쪽에도 연동 셋업이 필요해요. 이건 한 번만 하면 됩니다.

## 1. 코워크 데스크탑 앱 설치

[Claude.ai](https://claude.ai/download) 에서 데스크탑 앱 받아서 로그인. (코워크 mode 가 활성화된 계정이어야 합니다.)

## 2. 새 코워크 프로젝트 (Project) 하나 만들기

- 코워크 좌측 상단의 "+ New project" 클릭 → 프로젝트 이름 자유 (예: `쇼돈 예짜`)
- 만들어진 프로젝트 안으로 들어갑니다.

## 3. 작업 폴더를 `~/showdon/yejjas` 로 지정

코워크 프로젝트 화면 어딘가의 **폴더 선택 (Select folder)** 또는 **Add directory** 비슷한 메뉴 → Finder 가 뜨면 다음 경로 선택:

```
~/showdon/yejjas
```

(`~` 는 본인의 홈 폴더 — Finder 에서 보이지 않으면 `Cmd + Shift + G` 로 경로 직접 입력)

이 폴더가 코워크가 분석 결과를 읽고 편집점을 쓰는 작업장입니다. 예짜 앱이 분석 결과물을 여기다 저장하고, 코워크가 여기서 읽어가요.

## 4. 시스템 프롬프트 (System Prompt) 등록

같은 코워크 프로젝트 안의 **System Prompt** (또는 Custom Instructions / 지침) 메뉴로 이동 → 이 저장소의 다음 파일을 **통째로 복사** 해서 붙여넣고 저장:

```
~/showdon/showdon-yejja/SYSTEM_PROMPT_v1.9.1.md
```

(파일 이름 끝의 버전은 업데이트 때마다 바뀝니다 — 항상 **가장 최신 v1.x.x** 파일로 갱신.)

이 system prompt 가 코워크에게 "쇼돈 예짜 콘텐츠 디렉터 역할" 을 정의해줘요. 약 1,400 줄 / 79KB — 그대로 통째로 붙여넣으면 됩니다.

> **업데이트 시 기억**: 이 저장소가 `git pull` 로 갱신될 때마다 SYSTEM_PROMPT 파일도 새 버전이 들어옵니다. 그땐 코워크 프로젝트 system prompt 도 새 파일로 갈아끼워주세요. (그래야 최신 룰로 편집점이 작성됩니다.)

이 4 단계 끝나면 코워크 셋업 완료. 이제 쇼돈 예짜 앱을 쓸 준비 끝.

---

# 사용 흐름 (4 단계)

설치 + 코워크 셋업 끝나면 매번 이 순서로 작업하면 됩니다.

## ① 다운로드 — 예짜 앱의 [다운로드] 탭

- 영상 URL 을 붙여넣고 `[시작]` (YouTube · TikTok · Instagram · Threads · X 등 yt-dlp 호환)
- 결과물은 `~/showdon/yejjas/<영상명>/원본.mp4` 식으로 저장
- 같은 폴더 안에 분석 결과·편집점·완성본이 차곡차곡 쌓입니다

## ② 분석 — 같은 영상에서 [분석] 탭

- 원본 영상에 대해 STT (음성 인식) · scene cut 검출 · 얼굴 클러스터링 · 프레임 OCR 후보 추출이 자동으로 돌아갑니다
- M4 Pro 맥미니 추정 — 30분 영상 기준 약 15~25 분 (M5 Pro 측정값 +10%)
- 끝나면 `~/showdon/yejjas/<영상명>/분석/` 폴더에 `meta.json` · `stt.json` · `scene_cuts.json` · `face_clusters.json` · `ocr_candidates.json` · `frames/*.jpg` 가 생깁니다 → 이게 코워크가 읽을 입력

## ③ 편집점 요청 — [라이브러리] 탭에서 우클릭 → 코워크에게 요청

- 분석 끝난 영상은 **[라이브러리]** 탭에 카드로 뜹니다
- 카드 위에서 **우클릭** → `코워크에게 후보 추천 요청` (또는 `편집점 N번 요청`) 메뉴
- 메뉴를 누르면 **자동으로 코워크 프롬프트가 클립보드에 복사** + 코워크 데스크탑 앱이 활성화됩니다
- 코워크 프로젝트 채팅창에 **붙여넣기 (`Cmd + V`)** + Enter
- 잠깐 기다리면 후보 5~30개를 표 + JSON 으로 추천해줘요. 마음에 드는 후보를 골라 다시 "N번 만들어줘" 라고 한마디만 하면 코워크가 sub_cut · dialog 자막 · explain 자막 · 제목 · 해시태그를 모두 짜서 `~/showdon/yejjas/<영상명>/편집점/<날짜>_<제목>/` 폴더에 저장 (`_READY` 마커가 마지막에 박히면 작성 완료)

## ④ 자동편집 — 예짜 앱의 [자동편집] 탭에서 캡컷 모드 선택

- [자동편집] 탭 → 좌측에서 방금 코워크가 만든 편집점 폴더 (`_READY` 박힌 것) 선택
- **출력 모드** 에서 **캡컷 (CapCut)** 선택 (자동 렌더 모드도 있지만 캡컷 모드가 권장 — 뒤에서 손으로 트림·이펙트 추가 가능)
- `[시작]` 클릭 → CapCut Draft 가 자동 생성되어 캡컷 앱이 자동으로 그 Draft 를 열어줍니다
- 캡컷에서 마지막 검수·미세조정 → export

> **캡컷 Draft 자동 생성 작동 원리**: backend 가 내부 VectCutAPI 서버 (port 9000) 를 띄워서 CapCut 의 macOS Draft 폴더에 직접 트랙·자막·footer 를 박아줍니다. 첫 실행 시 capcut_server 가 떴다는 로그가 나옴. 캡컷 자체는 별도로 설치돼있어야 합니다 ([CapCut 다운로드](https://www.capcut.com/)).

---

## 업데이트 (이미 설치한 분)

위 1~2번 한 줄 명령어를 그대로 한 번 더 실행하면 자동으로 최신 버전으로 갱신됩니다.

업데이트 후엔 **코워크 프로젝트의 system prompt 도 최신 `SYSTEM_PROMPT_v1.x.x.md` 로 갈아끼워주세요** (룰이 자주 변합니다).

### Dock 아이콘 갱신 (Dock 에 끌어다 놓은 분)

빌드가 새로 돌면 `.app` 의 inode 가 바뀌어 macOS 가 **새 항목** 으로 인식 → Dock 에 아이콘이 하나 더 떠 있게 됩니다. 옛 아이콘은 누르면 동작 안 할 수 있어요.

정리:
1. **옛 아이콘** 우클릭 → **옵션 → Dock에서 제거**
2. Finder 에서 `~/showdon/showdon-yejja/dist/쇼돈 예짜.app` 을 다시 Dock 으로 끌어 놓기

## .app 만 다시 빌드하고 싶을 때

코드만 업데이트했거나 첫 install 에서 .app 빌드만 실패했으면, 아래 한 줄로 .app 만 다시 빌드 가능합니다:

```
cd ~/Downloads && rm -f 쇼돈_예짜_app_builder.command && curl -fsSL -o 쇼돈_예짜_app_builder.command "https://raw.githubusercontent.com/crack00000100000/showdon-yejja/main/%EC%87%BC%EB%8F%88_%EC%98%88%EC%A7%9C_app_builder.command" && chmod +x 쇼돈_예짜_app_builder.command && xattr -d com.apple.quarantine 쇼돈_예짜_app_builder.command 2>/dev/null; bash 쇼돈_예짜_app_builder.command
```

→ 자동으로 `git pull` + `make_app.sh` 실행해서 `~/showdon/showdon-yejja/dist/쇼돈 예짜.app` 갱신.

---

## 폴더 구조 한눈에

설치 끝나면 홈 폴더 (`~/showdon/`) 가 다음과 같아집니다:

```
~/showdon/
├── showdon-yejja/          ← 이 저장소 (앱 코드 + system prompt)
│   ├── dist/쇼돈 예짜.app   ← 더블클릭 실행
│   ├── SYSTEM_PROMPT_v1.x.x.md  ← 코워크 프로젝트에 붙여넣을 파일
│   ├── VectCutAPI/         ← CapCut Draft 생성 엔진 (내장)
│   ├── backend/  ui/  design/
│   └── README.md (이 파일)
└── yejjas/                 ← 작업 폴더 (코워크가 보는 곳)
    └── <영상명>/
        ├── 원본.mp4
        ├── 분석/           ← ② 분석 결과 (코워크 입력)
        ├── 편집점/         ← ③ 코워크가 작성한 폴더 (_READY)
        └── 완성/           ← ④ 자동편집 결과
```

`yejjas/` 가 **코워크 프로젝트 폴더** 와 같은 위치라는 점이 핵심 — 예짜 앱이 쓰고, 코워크가 읽고, 코워크가 쓰고, 예짜 앱이 읽는 공유 작업장입니다.

---

## 자주 묻는 질문

**Q. 코워크 (Claude) 가 없으면 못 쓰나요?**
A. 편집점 작성을 코워크가 하므로 — 코워크 데스크탑 앱 + 프로젝트 셋업이 필수입니다.

**Q. 캡컷 없이도 쓸 수 있나요?**
A. 자동편집 탭에 **자동 렌더 (ffmpeg)** 모드도 있어요. 다만 한국 쇼츠 트렌드에 맞춰 미세 트림·이펙트 추가하려면 캡컷 모드가 권장.

**Q. system_prompt 파일이 갱신되면 코워크에서 뭘 다시 해야 하나요?**
A. 코워크 프로젝트의 **System Prompt** 메뉴 열고 → 기존 내용 지우고 → 최신 `SYSTEM_PROMPT_v1.x.x.md` 통째로 붙여넣기 → 저장. 같은 채팅을 이어가도 새 룰이 즉시 적용됩니다.

**Q. 어떤 영상이 잘 맞나요?**
A. 길이 1~30 분 정도 토크·인터뷰·예능 영상. 한 화자가 길게 이야기하는 콘텐츠 (에픽하이·타블로·신동엽·유세윤 류) 가 sub_cut 추출이 잘 됩니다. 음악 영상이나 너무 빠른 컷 위주는 분석 어려움.
