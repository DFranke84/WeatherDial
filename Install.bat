@echo off
rem WeatherDial installer: installs what's missing (Python, pywebview), asks for your
rem location, Tempest token and optional keys (setup_config.py), then starts the panel.
rem Run it again any time to change those settings.  "Install.bat check" only reports.
setlocal
cd /d "%~dp0"
set CHECK=
if /i "%~1"=="check" set CHECK=1

echo.
echo  ===== WeatherDial installer =====
echo.

rem ---------------------------------------------------------------- Python
call :find_python
if defined PY (
    echo  [OK]  Python: %PY%
) else if defined CHECK (
    echo  [--]  Python: not installed
) else (
    echo  [..]  Python not found. Installing Python 3.14 ^(for your user only^)...
    winget install --id Python.Python.3.14 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
    call :find_python
    if not defined PY (
        echo  [!!]  Python install didn't finish. Install it from https://www.python.org and run this again.
        goto :end
    )
    echo  [OK]  Python installed: %PY%
)

rem ---------------------------------------------------------------- pywebview
"%PY%" -c "import webview" 2>nul
if not errorlevel 1 (
    echo  [OK]  pywebview
) else if defined CHECK (
    echo  [--]  pywebview: not installed
) else (
    echo  [..]  Installing pywebview...
    "%PY%" -m pip install --quiet --disable-pip-version-check pywebview
    "%PY%" -c "import webview" 2>nul && (echo  [OK]  pywebview installed) || (echo  [!!]  pywebview install failed.)
)

rem ---------------------------------------------------------------- your settings
if defined CHECK (
    if exist config.json (echo  [OK]  config.json ^(your location and station^)) else (echo  [--]  config.json: not set up yet)
    if exist keys.json (echo  [OK]  keys.json ^(optional keys^)) else (echo  [--]  keys.json: not set up yet)
    goto :end
)
if not exist config.json (
    "%PY%" setup_config.py
    if errorlevel 1 goto :end
) else (
    echo  [OK]  Settings found ^(config.json, keys.json^).
    choice /c YN /n /t 20 /d N /m "       Change them now? [Y/N] (N in 20 s) "
    if not errorlevel 2 "%PY%" setup_config.py
)

echo.
echo  Starting WeatherDial...
start "" "%PY:python.exe=pythonw.exe%" "%~dp0app.py"
goto :end


:find_python
set PY=
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%ProgramFiles%\Python314\python.exe"
    "C:\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "C:\Python313\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"
if defined PY exit /b
for /f "delims=" %%E in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%E"
exit /b


:end
echo.
if not defined CHECK pause
endlocal
