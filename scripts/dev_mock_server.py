"""Start the deterministic local capture fixture."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mock_site.server import MockSite  # noqa: E402

with MockSite() as server:
    sys.stdout.write(f"{server.url}/comic\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
