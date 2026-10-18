@echo off
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  echo First run setup:
  py -3.11 -m venv venv
  call venv\Scripts\activate
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate
)
python app.py
pause
