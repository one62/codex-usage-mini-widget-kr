# Codex Usage Mini Widget KR

Codex 계정의 사용량을 확인하는 한국어 Windows 미니 위젯입니다.

## 주요 기능

- 5시간·주간 사용률과 초기화까지 남은 시간·날짜를 표시합니다.
- Windows 배율에 맞춰 표시 폭을 계산하고, 사용량과 초기화 정보를 고정 폭으로 오른쪽 정렬합니다.
- 좌클릭은 뒤쪽 프로그램으로 전달하고, 우클릭으로 메뉴를 열거나 드래그하여 위젯을 이동합니다.
- 메뉴에서 투명도를 0~100%로 조절하고, 투명도와 위치를 저장하여 다음 실행 때 복원합니다.
- 1분마다 사용량을 갱신하며, 연결 실패 시 자동으로 재시도합니다.
- 메뉴에서 크레딧·리셋권·연결 상태를 확인하고, 확인창 승인 후 리셋권을 사용할 수 있습니다.
- 수동 새로고침, 항상 맨 위 표시, Windows 자동 실행, ChatGPT 열기와 종료를 지원합니다.

표시되는 사용률은 **사용한 비율**이며, 기본 투명도는 30%입니다.

## 실행

Windows 10/11, Tkinter를 포함한 Python 3.10 이상, 로그인된 Codex 데스크톱 앱 또는 CLI가 필요합니다.

`launch_widget.bat`를 더블클릭하거나 이 폴더에서 실행합니다.

```powershell
python widget.py
```

코드 변경 후에는 위젯을 종료하고 다시 실행합니다.

## 저장 파일

- 설정: `%LOCALAPPDATA%\CodexUsageMini\settings.json` (투명도·위치)
- 오류 로그: `%LOCALAPPDATA%\CodexUsageMini\widget.log`

사용량 조회는 설치된 Codex 클라이언트의 인증을 사용하며, 별도 API 키 입력은 필요하지 않습니다.

## 라이선스

[Wannatep/codex-usage-mini-widget](https://github.com/Wannatep/codex-usage-mini-widget)을 기반으로 하며, 원본의 [The Unlicense](UNLICENSE)를 유지합니다.

OpenAI의 공식 제품이 아닌 커뮤니티 프로젝트입니다.
