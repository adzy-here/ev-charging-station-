$env:DB_USER='root'
$env:DB_PASSWORD='notneeded4u'
$env:DB_NAME='ev_charging'
$env:JWT_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
venv\Scripts\activate
uvicorn main:app --reload --port 8000