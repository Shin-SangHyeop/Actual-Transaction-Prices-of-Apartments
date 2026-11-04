@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=C:\Users\ssh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Python executable was not found:
  echo %PYTHON_EXE%
  echo Check whether the Codex runtime path has changed.
  pause
  exit /b 1
)
"%PYTHON_EXE%" "%~dp0run.py"
if errorlevel 1 (
  echo.
  echo The app exited with an error.
  pause
)
endlocal
