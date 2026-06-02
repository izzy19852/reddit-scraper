@echo off
setlocal
set SCRIPT_DIR=%~dp0
set LOG=%SCRIPT_DIR%reddit_digest.log
set PATH=C:\Program Files\nodejs;%USERPROFILE%\AppData\Roaming\npm;%PATH%

REM Headless `claude` auth: Task Scheduler ("run whether logged on or not")
REM cannot read the interactive OAuth keychain, so the summarizer call fails
REM and the digest publishes WITHOUT the AI summary. Provide a key here (or via
REM the scheduled task's environment / a .env beside the script) so the
REM non-interactive `claude` call can authenticate.
REM   set ANTHROPIC_API_KEY=sk-ant-...

echo. >> "%LOG%"
echo === %DATE% %TIME% === >> "%LOG%"
REM --body-min 25: only fetch post bodies for posts with score>=25 or >=25
REM comments (Tier 1 always fetched), so heavy-result days don't balloon the
REM body-fetch phase. Lower it to widen body coverage, or 0 to fetch all.
"C:\Users\islam\AppData\Local\Microsoft\WindowsApps\python.exe" "%SCRIPT_DIR%reddit_digest.py" --body-min 25 >> "%LOG%" 2>&1
REM Exit code 3 = digest published but the AI summary was lost (claude failed).
if errorlevel 1 echo === exited with errorlevel %errorlevel% === >> "%LOG%"
endlocal
