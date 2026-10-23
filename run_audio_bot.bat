@echo off
setlocal
title KILLA'S BOSS audio bot
cd /d "%~dp0"

rem Launch KILLA'S BOSS with this folder's own virtual environment (.venv), which
rem holds discord.py, the auto-fit engine (librosa / soundfile / ShazamIO) and
rem pyacoustid from requirements.txt. One-time setup, from this folder:
rem     py -3.10 -m venv .venv
rem     .\.venv\Scripts\python.exe -m pip install -r requirements.txt
rem /reload restarts through sys.executable, so it stays inside .venv too.
set "BOT_PY=%~dp0.venv\Scripts\python.exe"

rem --- preflight: check BEFORE stopping a running bot, so a broken setup can't
rem leave you with no bot at all ------------------------------------------------
if not exist "%BOT_PY%" goto no_venv
"%BOT_PY%" -c "import discord, dotenv" >nul 2>&1
if errorlevel 1 goto no_packages
if exist "%~dp0.env" goto env_ok
if exist "%~dp0..\.env" goto env_ok
echo WARNING: no .env found in this folder. Copy your private .env back in here,
echo next to audio_bot.py, or the bot will stop with "DISCORD_TOKEN is not set".
echo.
:env_ok

rem --- clear out a leftover instance -----------------------------------------
rem Ctrl+C followed by "Terminate batch job (Y/N)" can leave python running with
rem no window attached. That orphan holds the bot's single-instance lock, so the
rem next launch would exit with "already running" -- which looks like the bot
rem refusing to refresh. Kill anything still running THIS script, matched on the
rem command line so nothing else on the machine (relay, watchdog, video bot) is
rem touched, then clear the recorded pid. This also catches an old instance
rem that was started with the previous system-wide Python. (The process-name
rem filter lives inside PowerShell: cmd strips an unpaired percent sign from a
rem .bat line, which silently broke the old WQL name filter so it never matched.)
echo Stopping any previous instance of the audio bot, if one is still running...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*audio_bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
del "%~dp0audio_bot.pid" >nul 2>&1

"%BOT_PY%" "%~dp0audio_bot.py"
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" goto clean
rem A Ctrl+C stop (3221225786 / 0xC000013A) or a closed console window
rem (1073741510 / 0xC000013A variants) is a normal stop, not a crash, so the
rem window closes instead of waiting to be dismissed.
if "%RC%"=="3221225786" goto clean
if "%RC%"=="-1073741510" goto clean
if "%RC%"=="1073741510" goto clean
echo Python exited with code %RC%. Read the message above, then press any key to close.
pause >nul
exit /b %RC%

:clean
echo KILLA'S BOSS stopped.
rem Bounded wait, so the last log lines stay readable -- but the window can never
rem sit here forever, which is what made a restart look refused. Ctrl+C during it
rem still closes immediately.
timeout /t 5 >nul 2>&1
exit /b 0

:no_venv
echo The bot's virtual environment was not found: .venv\Scripts\python.exe
echo Any running bot was left untouched. One-time setup, in this folder:
echo     py -3.10 -m venv .venv
echo     .\.venv\Scripts\python.exe -m pip install -r requirements.txt
echo Then run this launcher again.
pause >nul
exit /b 1

:no_packages
echo .venv exists, but discord.py / python-dotenv are not installed in it.
echo Any running bot was left untouched. In this folder, run:
echo     .\.venv\Scripts\python.exe -m pip install -r requirements.txt
echo Then run this launcher again.
pause >nul
exit /b 1
