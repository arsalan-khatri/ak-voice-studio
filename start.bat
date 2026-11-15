@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Pehli dafa setup ho raha hai...
  python -m venv .venv
)
call .venv\Scripts\activate
echo Packages check ho rahe hain...
pip install -q -r requirements.txt
if not exist .env if exist .env.example copy .env.example .env >nul
start "" http://127.0.0.1:8000
python app.py
pause
