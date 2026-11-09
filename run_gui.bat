@echo off
rem AWB console launcher. Pure ASCII; script path is resolved at runtime.
setlocal
set "ENTRY=%~dp0awb_gui.py"
if not exist "%ENTRY%" (
  echo Cannot find awb_gui.py next to this launcher.
  pause
  exit /b 1
)
where pythonw.exe >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw.exe "%ENTRY%"
) else (
  start "" python.exe "%ENTRY%"
)
endlocal
