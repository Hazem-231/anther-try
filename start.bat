@echo off
cd /d "%~dp0"
echo Starting QuantBot. Dashboard: http://127.0.0.1:8765
echo Close this window to stop the bot.
".venv\Scripts\python.exe" -u -m quantbot run
pause
