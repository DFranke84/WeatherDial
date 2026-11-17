@echo off
rem WeatherDial installer: installs what's missing (Python, pywebview), creates your
rem config.json and keys.json, then starts the panel.  "Install.bat check" only reports.
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
if exist config.json (echo  [OK]  config.json ^(your location^)) else if defined CHECK (
    echo  [--]  config.json: not created yet
) else (
    copy /y config.example.json config.json >nul
    echo  [OK]  config.json created: put in your latitude and longitude ^(opening it now^)
    start "" notepad.exe config.json
)
if exist keys.json (echo  [OK]  keys.json ^(API keys^)) else if defined CHECK (
    echo  [--]  keys.json: not created yet
) else (
    copy /y keys.example.json keys.json >nul
    echo  [OK]  keys.json created: optional free keys, see README
)
if defined CHECK goto :end

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
