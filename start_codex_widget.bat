@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "WIDGET_DIR=%ROOT_DIR%codex-usage-widget"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name = 'pythonw.exe' OR Name = 'python.exe'\" | Where-Object { $_.CommandLine -match 'codex-usage(-mini)?-widget(-main)?[\\/]+widget\.py' }; foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Wait-Process -Id $p.ProcessId -Timeout 5 -ErrorAction SilentlyContinue }"

where pythonw.exe >nul 2>nul
if errorlevel 1 (
  start "" /d "%WIDGET_DIR%" /b python "%WIDGET_DIR%\widget.py"
) else (
  start "" /d "%WIDGET_DIR%" /b pythonw.exe "%WIDGET_DIR%\widget.py"
)
endlocal
