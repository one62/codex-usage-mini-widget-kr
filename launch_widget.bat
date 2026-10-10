@echo off
setlocal
set "WIDGET_DIR=%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
  python "%WIDGET_DIR%widget.py"
) else (
  start "" /b pythonw.exe "%WIDGET_DIR%widget.py"
)
endlocal

