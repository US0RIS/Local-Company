@echo off
setlocal
cd /d "%~dp0"

rem Prefer Python 3.12, the backend's primary compatibility target.
py.exe -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if not errorlevel 1 goto PY312

py.exe -3.13 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if not errorlevel 1 goto PY313

py.exe -3.14 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if not errorlevel 1 goto PY314

python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if not errorlevel 1 goto PYTHON

echo Python 3.12 or newer is required.
echo Install it with: winget install -e --id Python.Python.3.12
exit /b 1

:PY312
echo Starting Local Company with Python 3.12...
py.exe -3.12 "%~dp0start_windows.py"
goto DONE

:PY313
echo Starting Local Company with Python 3.13...
py.exe -3.13 "%~dp0start_windows.py"
goto DONE

:PY314
echo Starting Local Company with Python 3.14...
py.exe -3.14 "%~dp0start_windows.py"
goto DONE

:PYTHON
echo Starting Local Company with python.exe...
python.exe "%~dp0start_windows.py"

:DONE
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
  echo.
  echo Local Company exited with code %EXITCODE%.
)
exit /b %EXITCODE%
