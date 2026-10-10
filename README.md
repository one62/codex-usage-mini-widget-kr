# Codex Usage Mini Widget KR

Codex 계정의 사용량을 확인하는 Windows용 미니 위젯입니다. 원본 프로젝트를 기반으로 한국어 UI, 마우스 조작과 사용량 표시를 개선한 별도 저장소입니다.

**원본 저장소:** [Wannatep/codex-usage-mini-widget](https://github.com/Wannatep/codex-usage-mini-widget)

![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-7CB9E8)
![Python](https://img.shields.io/badge/Python-3.10%2B-F7DB83)
![License](https://img.shields.io/badge/license-Unlicense-8FE3B2)

## 현재 상태

현재 코드에 다음 동작이 구현되어 있습니다. 실제 Windows에서의 동작은 실행 환경에 따라 직접 확인해야 합니다.

- 5시간(`5H`) 및 주간(`WK`) 사용률, 5시간 한도의 초기화까지 남은 시간 표시
- 사용률에 따른 파스텔 색상 표시
- 높이 38px, 최소 너비 270px(배율과 표시 내용에 따라 자동 확장), 불투명도 70%, 기본적으로 다른 창 위에 표시
- 좌클릭과 좌클릭 드래그를 뒤쪽 프로그램으로 통과시킴
- 우클릭으로 메뉴 열기, 우클릭 드래그로 위젯 이동
- 메뉴 바깥 클릭 시 메뉴 닫기
- Windows 배율에 따른 마우스 좌표 불일치를 막기 위한 모니터별 DPI 처리
- 우클릭 훅을 전용 스레드에서 처리
- 오른쪽 끝에 초기화까지 남은 시간을 분 단위까지 표시하고, 내용이 잘리지 않도록 너비 자동 조정
- 작업 표시줄을 제외한 모니터 작업 영역 안으로 이동 위치 제한
- 메뉴에서 수동 새로고침, 항상 맨 위 표시, Windows 자동 실행, 연결 상태 확인, 앱 열기, 종료 지원

초기화까지 남은 시간은 `↻ 4h 23m`, `↻ 30m`, 하루 이상이면 `↻ 1d 2h 15m` 형식입니다. 1분 미만의 남은 시간은 1분으로 올림하여 표시합니다. 왼쪽 아이콘과 화면의 새로고침·종료 버튼은 제거했습니다.

숫자와 막대는 **남은 비율이 아닌 사용한 비율**을 나타냅니다. 크레딧은 우클릭 메뉴에서 확인할 수 있습니다.

```text
5H 12% ▮▯▯▯▯▯  |  WK 28% ▮▮▯▯▯▯  ↻ 4h 23m
```

메뉴와 위젯에서 제공하는 연결 상태, 오류 안내 메시지는 한국어로 표시합니다. Codex나 Windows에서 반환하는 오류 상세 내용은 해당 프로그램의 원문으로 표시될 수 있습니다.

## 실행 환경

- Windows 10 또는 Windows 11
- Tkinter를 포함한 Python 3.10 이상
- Codex 실행 파일이 포함된 데스크톱 앱 또는 Codex CLI
- ChatGPT 계정으로 Codex에 로그인한 상태

사용량 조회는 설치된 Codex 클라이언트의 인증을 사용합니다. 별도로 API 키를 입력할 필요가 없습니다.

## 실행 방법

`launch_widget.bat`를 더블클릭하거나, 이 폴더에서 다음 명령을 실행합니다.

```powershell
python widget.py
```

배치 파일은 `pythonw.exe`가 있으면 콘솔 창 없이 실행하고, 없으면 `python`으로 실행합니다.

코드를 수정한 뒤에는 실행 중인 위젯을 종료하고 다시 실행해야 변경 사항이 적용됩니다.

## 마우스 조작

| 조작 | 동작 |
| --- | --- |
| 위젯 위에서 좌클릭 | 뒤쪽 프로그램으로 클릭 전달 |
| 위젯 위에서 좌클릭 드래그 | 뒤쪽 프로그램으로 드래그 전달 |
| Ctrl을 누른 상태에서 좌클릭 | 뒤쪽 프로그램으로 클릭 전달 |
| 위젯 위에서 우클릭 후 버튼 놓기 | 위젯 메뉴 표시 |
| 위젯 위에서 우클릭 드래그 | 위젯 이동, 버튼을 놓아도 메뉴가 열리지 않음 |
| 메뉴 안에서 좌클릭 | 해당 메뉴 항목 선택 |
| 메뉴 바깥에서 클릭 | 메뉴 닫기 |

새로고침과 종료는 우클릭 메뉴를 사용합니다. 위젯에 키보드 포커스가 있을 때는 `Esc`로 종료할 수도 있습니다.

클릭이 동작하지 않아 종료할 수 없다면 작업 관리자에서 해당 위젯의 `pythonw.exe` 또는 `python.exe` 프로세스를 확인해 종료합니다. 다른 Python 프로그램도 실행 중이라면 실행 경로와 명령줄의 `widget.py`를 확인해 구분합니다.

## 사용량 조회와 연결 복구

`codex_usage.py`는 로컬 Codex App Server를 표준 입출력으로 실행하고 `account/rateLimits/read` 응답을 읽습니다. 위젯이 직접 `auth.json`, 브라우저 쿠키, 비밀번호, API 키를 읽거나 사용량 이력을 파일로 저장하지는 않습니다.

- 1분마다 사용량 자동 조회
- 초기화까지 남은 시간도 1분마다 화면에서 갱신
- Codex 업데이트로 실행 파일 경로가 바뀌면 현재 설치 경로를 다시 탐색
- 일시적인 연결 실패 시 마지막 사용량 값을 유지하고 `retry` 표시
- 기존 값이 없거나 실패가 반복되면 `offline` 표시
- 실패 횟수에 따라 15초, 30초, 60초, 120초, 이후 최대 300초 간격으로 재시도
- 조회가 50초 이상 끝나지 않으면 대기 상태를 해제하고 다시 조회할 수 있도록 복구

연결 오류는 다음 파일에 기록됩니다. 로그는 크기 제한과 순환 보관이 적용되며, 위젯은 인증 정보나 사용량 이력을 기록하지 않습니다.

```text
%LOCALAPPDATA%\CodexUsageMini\widget.log
```

우클릭 메뉴의 **연결 상태**에서 최근 오류와 로그 경로를 확인할 수 있습니다.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `widget.py` | 위젯 화면, 마우스 훅, 메뉴, 이동, 자동 실행 및 새로고침 처리 |
| `codex_usage.py` | Codex 실행 파일 탐색, App Server 통신, 사용량 응답 해석 |
| `launch_widget.bat` | Windows 실행용 배치 파일 |
| `tests/test_widget.py` | 사용량 해석, 마우스 조작, DPI 및 Windows 팝업 검증 |
| `UNLICENSE` | 라이선스 원문 |

## 개발 및 검증

`tests/test_widget.py`에는 Windows API와 Tk 팝업을 사용하는 테스트가 포함되어 있습니다. 아래 명령은 Windows에서 개발자가 직접 실행하는 검증용 명령입니다. 현재 저장소 업로드 과정에서는 위젯 실행, 자동 테스트 및 실행 파일 빌드를 수행하지 않았습니다.

실제 마우스 조작은 위젯 실행 후 좌클릭 통과, 우클릭 메뉴, 우클릭 드래그, 메뉴 바깥 클릭을 직접 확인합니다.

```powershell
python -m unittest discover -s tests -v
python -m py_compile widget.py codex_usage.py tests/test_widget.py
```

## 실행 파일 만들기

PyInstaller로 콘솔 창 없는 단일 실행 파일을 만들 수 있습니다.

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name CodexUsageMini widget.py
```

결과 파일은 `dist\CodexUsageMini.exe`입니다. 빌드 후 실제 Windows 환경에서 실행을 확인해야 합니다.

## 프로젝트 안내 및 라이선스

이 프로젝트는 OpenAI의 공식 제품이 아닌 독립적인 커뮤니티 프로젝트입니다. ChatGPT, Codex, OpenAI는 각 소유자의 상표이며, 이 저장소에는 OpenAI의 공식 로고 이미지를 포함하지 않습니다.

이 프로젝트는 [Wannatep/codex-usage-mini-widget](https://github.com/Wannatep/codex-usage-mini-widget)을 기반으로 합니다. 원작자는 Wannatep이며, 원본의 `UNLICENSE` 파일을 보존합니다. 이 저장소도 [The Unlicense](UNLICENSE)를 따릅니다.

원본 대비 주요 변경 사항은 한국어 메뉴와 오류 안내, 좌클릭 통과 및 우클릭 이동, DPI 좌표 처리, 분 단위 초기화 시간 표시, 표시 너비 자동 조정, 1분 간격 사용량 갱신입니다.
