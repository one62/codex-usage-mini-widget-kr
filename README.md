# AI Usage Mini Widgets

Codex(ChatGPT)와 Gemini(Antigravity) 사용량을 바탕화면에 작게 띄워 확인하는 Windows 미니 위젯 모음입니다.

## 폴더 구성

- `common/`: 두 위젯이 함께 사용하는 설정 저장(`settings.py`), 창·후킹·트레이/메뉴·공통 UI(`widget_base.py`), 넥슨 풋볼고딕 폰트(`assets/fonts/`)
- `codex-usage-widget/`: Codex 사용량 미니 위젯 ([상세 안내](codex-usage-widget/README.md))
- `gemini-usage-widget/`: Gemini / Antigravity 사용량 미니 위젯 ([상세 안내](gemini-usage-widget/README.md))

## 실행 및 재시작

루트 폴더의 배치 파일을 실행하면 해당 위젯을 실행합니다. 이미 같은 위젯이 실행 중이면 기존 프로세스를 종료한 뒤 다시 시작합니다.

- Codex 위젯: `start_codex_widget.bat`
- Gemini 위젯: `start_gemini_widget.bat`

직접 Python으로 실행할 수도 있습니다.

```powershell
python codex-usage-widget\widget.py
python gemini-usage-widget\widget.py
```

## 라이선스

[Wannatep/codex-usage-mini-widget](https://github.com/Wannatep/codex-usage-mini-widget)을 기반으로 하며, 루트의 [The Unlicense](UNLICENSE)를 유지합니다.

동봉한 넥슨 풋볼고딕의 [저작권·이용 조건](common/assets/fonts/NEXON-LICENSE.txt)과 [출처 안내](common/assets/fonts/README.md)를 함께 제공합니다.
