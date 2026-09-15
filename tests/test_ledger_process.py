from __future__ import annotations

import subprocess
import sys

from quire.store.ledger import Ledger
from scripts.smoke_ledger import run_smoke


def test_real_process_kill_and_cache_recovery(tmp_path):
    report = run_smoke(tmp_path / "state")
    assert report == {
        "killed_exit_code": -9,
        "reused_after_kill": 1,
        "retried_after_kill": 2,
        "invalidated_after_corruption": 1,
    }


def test_second_process_cannot_open_active_ledger(tmp_path):
    with Ledger(tmp_path):
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from quire.store.ledger import Ledger; "
                "from quire.errors import LedgerError\n"
                "try:\n with Ledger(Path(sys.argv[1])): pass\n"
                "except LedgerError:\n sys.exit(7)\n",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert process.returncode == 7, process.stderr
    with Ledger(tmp_path):
        pass
