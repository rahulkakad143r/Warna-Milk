# Run Guide - Warana Milk

## Prerequisites
- Python 3.11+
- Windows (tested) or Linux/Mac

## Setup
1. Activate venv: \.\\venv\\Scripts\\activate\
2. Install deps: \pip install -r requirements.txt\
3. Migrate: \python manage.py migrate\
4. Seed data: \python manage.py seed_data\

## Run
- Dev server: python manage.py runserver
- Access: http://127.0.0.1:8000/
- Admin: http://127.0.0.1:8000/admin/

## Test & Verify
- Check: \python manage.py check\
- Tests: \python manage.py test\ (78 tests pass)
- Compile: \python -m compileall . -q\

## Environment Variables (MySQL)
- MYSQL_DATABASE
- MYSQL_USER
- MYSQL_PASSWORD
- MYSQL_HOST (default 127.0.0.1)

