@echo off
setlocal
set SCRIPT_DIR=%~dp0
set LOG=%SCRIPT_DIR%reddit_digest.log
set PATH=C:\Program Files\nodejs;%USERPROFILE%\AppData\Roaming\npm;%PATH%
echo. >> "%LOG%"
echo === %DATE% %TIME% === >> "%LOG%"
"C:\Users\islam\AppData\Local\Microsoft\WindowsApps\python.exe" "%SCRIPT_DIR%reddit_digest.py" >> "%LOG%" 2>&1
endlocal
