# 수업 영상 편집실 명령

저장소 루트에서 `PY tools/cvs.py <명령>`을 실행해요. `PY`는 맥에서 `.venv/bin/python`, 윈도우에서 `.venv\Scripts\python.exe`예요. 한글이나 공백이 있는 경로는 큰따옴표로 감싸 주세요. 명령 뒤에 `--help`를 붙이면 사용법을, `--debug`를 붙이면 오류의 자세한 내용을 볼 수 있어요.

| 명령 | 하는 일 |
| --- | --- |
| `doctor [--fetch-model]` | Python, FFmpeg, 받아쓰기 백엔드, 얼굴 모델, 한글 글꼴을 확인해요. |
| `init <작업>` | 작업 폴더와 빈 조건표, 편집안을 만들어요. |
| `fetch <작업> <링크 또는 파일...> [--move]` | 구글 드라이브나 로컬 영상 파일을 `원본/`에 넣어요. 파일은 기본으로 복사해요. |
| `probe <작업>` | 원본을 자연 정렬하고 표시 방향, 길이, 수업 전체에서의 시작 시각을 읽어요. |
| `readdoc <파일> [--out <파일>]` | PDF, HWP, HWPX, DOCX, TXT, Markdown 문서의 글을 읽어요. |
| `transcribe <작업> [--model <모델>] [--force]` | 수업에서 한 말을 컴퓨터에서 받아써요. |
| `analyze <작업>` | 조용한 구간, 장면이 바뀐 시각, 30초마다 소리 크기와 말한 비율을 찾아요. |
| `faces-grid <작업> [--every 60]` | 화면을 최대 12장 뽑아 전체를 모자이크하고, 찾은 얼굴(노란 상자)과 교사 위치를 고를 3×3 칸을 표시해요. |
| `faces-grid <작업> --plan` | 편집안 구간마다 가운데 화면을 한 장씩 뽑아 `plan/얼굴확인-구간.jpg`에 모아요. 초록 테두리는 교사 칸, 초록 상자는 교사로 볼 얼굴, 노란 상자는 흐릴 얼굴이에요. |
| `span <작업> <14:17-15:34> [--id c03] [--stage 단계]` | 수업 시각을 받아쓴 문장 경계에 맞춰 편집안 구간(원본, 원본 안 시각)으로 바꾸고, 그 안의 말을 보여 줘요. 마지막 줄이 편집안에 넣을 JSON이에요. |
| `conti <작업> [--resolved c03,c05] [--reset-notes]` | 컷 미리보기, 대본, 콘티, 완성본 예상 시각을 만들어요. 컷 ID가 같으면 메모와 상태를 이어받고, `--resolved`로 넘긴 컷은 반영했으니 비워요. |
| `conti-notes <작업>` | 고쳐 달라는 컷, 메모가 있는 컷, 콘티에서 빠진 컷을 `plan/콘티메모.json`에 정리해요. |
| `render <작업> [--draft] [--out <이름>]` | 편집안을 조건표와 대조한 뒤 `out/`에 영상으로 만들어요. 시안은 긴 변 640px이고 이름 끝에 `_시안`이 붙어요. |
| `check <작업> [파일]` | 영상과 조건표를 대조하고 `out/점검.md`, `out/점검/모음.jpg`에 결과를 써요. 파일을 생략하면 최근 제출본을 확인해요. |

## 조건표와 편집안

`plan/조건표.json`은 요강을 읽고 채워요. `null`은 요강에서 찾지 못한 값이에요. 사람이 확인하면 `confirmed`를 `true`로 바꿔 주세요.

```json
{
  "contest": "수업발표대회",
  "source": "요강/공문.pdf",
  "confirmed": false,
  "duration": {"min_sec": null, "max_sec": 600, "target_sec": 570},
  "format": {"ext": "mp4", "vcodec": "h264", "resolution": "1920x1080", "fps": null},
  "max_mb": 500,
  "filename": "수업영상",
  "captions": "allowed",
  "edit": {"speedup": false, "stage_label": true, "clock_label": true, "narration": false},
  "blur_faces": true,
  "anonymity": {"school": true, "teacher": false},
  "notes": ["학교명이 보이는 게시물을 가려요."]
}
```

확장자는 `mp4`, `mov`, `mkv`, `avi`, `wmv`를 쓸 수 있어요. `max_mb`가 있으면 용량에 맞춰 두 번 인코딩해요. `captions`에는 `allowed`, `forbidden`, `required`를 써요. 자막이 필수라면 자막을 넣을 수 있는 편집 도구가 필요해요.
`anonymity.teacher`는 교사 얼굴까지 가려야 할 때만 `true`예요. 교사 이름만 숨기라는 요강이면 `false`로 두고 이름표나 게시물 가리기를 `notes`와 `masks`로 챙겨요.

`plan/편집안.json`의 시작과 끝 시각은 해당 원본 안에서의 초예요. 구간을 수업 순서대로 적고, 겹치지 않게 골라 주세요.

```json
{
  "version": 1,
  "title": "광합성 실험 수업",
  "options": {
    "crossfade": 0.4,
    "stage_label": true,
    "clock_label": true,
    "blur_faces": true,
    "blur_style": "blur",
    "teacher_cells": ["B1", "B2"],
    "masks": [],
    "blur_cells": [],
    "loudnorm": -16
  },
  "segments": [
    {"id": "c01", "src": "A", "start": 312, "end": 398.5, "stage": "도입", "note": "첫 질문", "speed": 1},
    {"id": "c02", "src": "A", "start": 905, "end": 1105, "stage": "전개", "note": "모둠 활동", "speed": 4}
  ],
  "narration": []
}
```

- `speed`는 기본 1이고 2~8배속을 쓸 수 있어요. 제출 영상에서 빠르게 재생하는 구간은 수업 소리를 꺼요.
- 교사 칸은 A1~C3이에요. 교사 칸 안에서는 맨 위 얼굴 하나(서 있는 교사)만 흐리지 않고, 같은 칸에 앉은 학생은 흐려요. `teacher_zones`로 직접 지정하거나 `masks`로 이름표 같은 곳을 가릴 수 있어요. 상자 하나는 `[왼쪽, 위, 오른쪽, 아래]`를 0~1로 적어요. 각 구간에서도 교사 영역이나 가림 상자를 따로 지정할 수 있어요.
- `blur_cells`에 칸 이름(예: `["A3", "B3", "C3"]`)을 적으면 그 칸을 늘 통째로 흐려요. 얼굴 찾기는 옆얼굴과 작은 얼굴을 놓칠 수 있으니, 카메라가 고정이고 대회가 학생 얼굴을 막으면 학생 자리 칸을 여기에 적어요. 구간마다 따로 적을 수도 있어요.
- 해설을 넣으려면 `narration`에 `{"cut":"c02","file":"audio/해설-02.wav","at":0,"duck_db":-14}`를 추가해요. `at`은 완성된 컷 안에서 해설이 시작할 시각이에요.

## 결과 파일

`plan/원본.json`은 원본별 정보와 수업 시간줄의 시작점(`offset`)을, `받아쓰기.json`은 원본 안 시각(`t0`, `t1`)과 수업 시간줄의 시각(`start`, `end`)을 담아요. `분석.json`의 시각은 수업 시간줄 기준이에요.

[콘티 작업실](https://n-lifescience.github.io/conti-studio/)에서 `conti`를 실행한 작업 폴더를 열어요. `script/V1.json`은 대본, `conti/V1.json`은 컷, `conti/V1-timeline.json`은 배속과 이음매를 반영한 시각, `media/clips/`는 소리가 있는 로컬 미리보기예요. 미리보기에는 얼굴 흐림이 들어가지 않으니 수업 영상과 함께 작업 폴더 안에서만 확인해 주세요.

점검에서 교사 영역 밖에 얼굴을 찾으면 `out/점검/얼굴의심.jpg`에 모자이크한 화면과 빨간 상자, 시각을 남겨요. `모음.jpg`도 화면 전체를 모자이크하고 꼬리표 자리만 남겨요(AI가 열어 보는 그림이라서요). 얼굴 탐지에서 놓친 얼굴, 이름표, 학교명은 제출 전에 영상에서 확인해 주세요.
