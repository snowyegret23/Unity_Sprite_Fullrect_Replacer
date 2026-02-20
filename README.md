[> for English version of README.md](README_EN.md)

# Unity Sprite FullRect Replacer

Unity Sprite 추출/교체 도구입니다.  
본 문서는 **PyInstaller로 빌드된 exe 사용 기준**으로 작성되어 있습니다.

## 주요 기능

1. Sprite 정보 추출(JSON)
2. JSON 기반 Sprite 교체
3. Sprite 전체 추출(PNG + JSON)
4. Sprite 일괄 Import(변경분만 반영, 없는 파일은 기본 스킵)
5. 필터 추출
   - 파일명 + PathID 기반(`--ids`)
   - 이름 기반(`--names`, `--name-contains`)
6. 교체 모드
   - `fullrect`: FullRect 강제
   - `tightclip`: 이미지 알파 영역 기준 bbox로 타이트 클리핑

## 실행 파일 준비

배포판을 받았으면 아래 exe를 바로 실행하시면 됩니다.

- `unity_sprite_fullrect_replacer.exe`
- `unity_sprite_fullrect_replacer_en.exe`
- `UABEA_sprite_json_edit.exe`
- `UABEA_sprite_json_edit_en.exe`

소스에서 직접 빌드하는 경우:

```bash
py -3.12 -m pip install -U pyinstaller UnityPy Pillow
py -3.12 -m PyInstaller --onefile --name unity_sprite_fullrect_replacer unity_sprite_fullrect_replacer.py
py -3.12 -m PyInstaller --onefile --name unity_sprite_fullrect_replacer_en unity_sprite_fullrect_replacer_en.py
py -3.12 -m PyInstaller --onefile --name UABEA_sprite_json_edit UABEA_sprite_json_edit.py
py -3.12 -m PyInstaller --onefile --name UABEA_sprite_json_edit_en UABEA_sprite_json_edit_en.py
```

빌드 후 exe 위치:

```text
dist\unity_sprite_fullrect_replacer.exe
dist\unity_sprite_fullrect_replacer_en.exe
dist\UABEA_sprite_json_edit.exe
dist\UABEA_sprite_json_edit_en.exe
```

## CLI 레퍼런스 (unity_sprite_fullrect_replacer.exe)

기본 형식:

```bat
unity_sprite_fullrect_replacer.exe [옵션]
```

전체 옵션 목록(빠짐없이):

- `--gamepath PATH`
  - 게임 루트 / `_Data` / 단일 `.assets` 파일 경로
  - 기본값: 없음(미지정 시 실행 중 입력)
- `--parse`
  - Sprite 메타 JSON 추출
  - 기본값: `False`
- `--extract-all`
  - Sprite PNG 전체(또는 필터 대상) 추출 + JSON 생성(옵션)
  - 기본값: `False`
- `--list JSON_FILE`
  - JSON 기반 Sprite 교체
  - 기본값: 없음
- `--ids IDS`
  - 파일명+PathID 필터, 콤마/반복 지정 가능
  - 예: `--ids "sharedassets0.assets:186,sharedassets0.assets:200"`
  - 기본값: 없음
- `--name NAME`
  - Sprite 이름 필터 (반복/콤마 지정 가능)
  - 기본값: 없음
- `--names NAME`
  - `--name`과 동일한 별칭
  - 기본값: 없음
- `--name-contains TEXT`
  - Sprite 이름 부분일치 필터 (반복/콤마 지정 가능)
  - 기본값: 없음
- `--mode fullrect|tightclip`
  - 교체 모드
  - 기본값: `fullrect`
- `--output-dir PATH`
  - `--extract-all` PNG 출력 폴더
  - 기본값: `<스크립트폴더>\<게임이름>_sprites`
- `--json-out PATH`
  - `--parse`/`--extract-all` JSON 출력 파일 경로
  - 기본값: `<스크립트폴더>\<게임이름>_sprites.json`
- `--skip-missing`
  - `Replace_to` 파일이 없어도 스킵하고 계속 진행
  - 기본값: `True`
- `--no-skip-missing`
  - `Replace_to` 파일이 없으면 오류로 중단
  - 기본값: `False`
- `--changed-only`
  - 실제 변경분만 반영
  - 기본값: `True`
- `--no-changed-only`
  - 동일 데이터여도 강제 재기록
  - 기본값: `False`
- `--verbose`
  - 콘솔 로그를 `verbose.txt`에도 저장
  - 기본값: `False`

실행 예시:

1. Sprite 메타 JSON 추출
```bat
unity_sprite_fullrect_replacer.exe --gamepath "D:\Games\Game" --parse
```

2. 전체 Sprite PNG 추출 + JSON 생성
```bat
unity_sprite_fullrect_replacer.exe --gamepath "D:\Games\Game" --extract-all --output-dir ".\sprites_out" --json-out ".\sprites.json"
```

3. 파일명+PathID 필터 추출
```bat
unity_sprite_fullrect_replacer.exe --gamepath "D:\Games\Game_Data\sharedassets0.assets" --extract-all --ids "sharedassets0.assets:186"
```

4. 이름 필터 추출 (`--name` / `--names` 동일)
```bat
unity_sprite_fullrect_replacer.exe --gamepath "D:\Games\Game" --extract-all --name "字 A"
unity_sprite_fullrect_replacer.exe --gamepath "D:\Games\Game" --extract-all --names "字 A"
```

5. JSON 기반 교체 (fullrect)
```bat
unity_sprite_fullrect_replacer.exe --gamepath "D:\Games\Game" --list ".\sprites.json" --mode fullrect
```

6. JSON 기반 교체 (tightclip)
```bat
unity_sprite_fullrect_replacer.exe --gamepath "D:\Games\Game" --list ".\sprites.json" --mode tightclip
```

## JSON 형식

`--parse` 또는 `--extract-all`로 생성한 JSON을 수정해서 `--list`에 넣으면 됩니다.

```json
{
  "sharedassets0.assets|sharedassets0.assets|字 A|Sprite|186": {
    "Type": "Sprite",
    "File": "sharedassets0.assets",
    "assets_name": "sharedassets0.assets",
    "Path_ID": 186,
    "Name": "字 A",
    "TextureRect_X": 125,
    "TextureRect_Y": 369,
    "TextureRect_Width": 468,
    "TextureRect_Height": 696,
    "Mode": "fullrect",
    "Replace_to": "C:\\path\\to\\字 A.sprite.png"
  }
}
```

## UABEA JSON 보정 스크립트 (UABEA_sprite_json_edit.exe / UABEA_sprite_json_edit_en.exe)

`UABEA_sprite_json_edit.exe`(한국어), `UABEA_sprite_json_edit_en.exe`(영어)는 UABEA로 덤프한 Sprite JSON을 FullRect 기준으로 보정합니다.

역할:
- UABEA로 덤프한 Sprite JSON을 FullRect 기준으로 보정
- `m_RD.settingsRaw`를 FullRect 비트로 변경
- `m_RD.textureRect` / `textureRectOffset`를 `m_Rect` 기준으로 확장

동작 규칙:
- **인자 있음**: `<원본이름>.fullrect.json` 파일 생성 (원본 유지)
- **인자 없음**: 현재 폴더(또는 `--dir`)의 JSON 중 `.fullrect.json` 제외 파일을 직접 수정

CLI 옵션(전체):

- `inputs` (위치 인자, 0개 이상)
  - 지정 시 각 입력 JSON으로부터 `<이름>.fullrect.json` 생성
  - 기본값: 없음
- `--dir PATH`
  - 인자 없을 때 배치 대상 폴더
  - 기본값: 현재 폴더(`.`)
- `--recursive`
  - 인자 없을 때 하위 폴더까지 재귀 탐색
  - 기본값: `False`
- `--no-expand-rect`
  - `textureRect`/`textureRectOffset`를 `m_Rect`로 확장하지 않음
  - 기본값: `False` (즉, 기본은 확장)

예시:

1. 단일 파일 입력 -> `<이름>.fullrect.json` 생성
```bat
UABEA_sprite_json_edit.exe "字 A-sharedassets0.assets.bak_before_sprite_replace-186.json"
```

2. 현재 폴더 일괄 수정 (`.fullrect.json` 제외)
```bat
UABEA_sprite_json_edit.exe
```

3. 특정 폴더 일괄 수정
```bat
UABEA_sprite_json_edit.exe --dir "C:\path\to\json_folder"
```

4. 특정 폴더 재귀 일괄 수정
```bat
UABEA_sprite_json_edit.exe --dir "C:\path\to\json_folder" --recursive
```

## 참고

- `Managed` 폴더는 Sprite 수정 작업에 필요하지 않습니다.
- 입력 경로는 게임 루트 / `_Data` / 단일 `.assets` 파일 모두 지원합니다.
- `fullrect` 교체 시 `settingsRaw`와 함께 `textureRect/textureRectOffset`도 `m_Rect` 기준으로 맞춰 적용합니다.
- `--gamepath`에 게임 루트 또는 `_Data`를 줄 때는 `_Data` 최상위 `.assets`를 우선 처리합니다.
  (하위 폴더의 `Original`/`backup` 복사본 `.assets`는 기본 스캔에서 제외됩니다.)
- Texture2D 로드 중 `.resS`가 누락된 항목은 프로그램 전체 중단 대신 해당 항목만 스킵합니다.

## 라이선스

[MIT License](LICENSE)
