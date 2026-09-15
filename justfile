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
