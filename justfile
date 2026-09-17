python := ".venv/bin/python"

test:
    {{python}} -m pytest

lint:
    .venv/bin/ruff check .
    .venv/bin/ruff format --check .

type:
    .venv/bin/mypy src/quire

check: lint type test

pyz:
    {{python}} scripts/build_micro_zipapp.py

doctor: pyz
    {{python}} -S dist/quire.pyz doctor

mock:
    {{python}} scripts/dev_mock_server.py

ledger-smoke:
    {{python}} scripts/smoke_ledger.py

session-smoke:
    {{python}} scripts/smoke_session.py

resume-smoke:
    {{python}} scripts/smoke_resume.py

compression-smoke:
    {{python}} scripts/smoke_compression.py

formats-smoke:
    {{python}} scripts/smoke_formats.py

app:
    scripts/build_app.sh

dmg: app
    scripts/make_dmg.sh
