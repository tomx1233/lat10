@echo off
setlocal
title Stop KILLA'S BOSS
cd /d "%~dp0"

rem Stop the audio bot from anywhere -- useful when its window is gone but the
rem process is still alive (Ctrl+C + "Terminate batch job" can leave it running),
rem which is exactly the state that made a restart say "already running".
rem Matched on the command line, so only this script is killed.
echo Stopping KILLA'S BOSS...
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*audio_bot.py*' }; if (-not $procs) { Write-Host 'No audio bot process is running.' } else { $procs | ForEach-Object { Write-Host ('Stopping pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }"
del "%~dp0audio_bot.pid" >nul 2>&1
echo Done.
pause >nul
