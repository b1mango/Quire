from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"


def test_layer_boundaries_and_module_sizes():
    for path in (ROOT / "quire").rglob("*.py"):
        code = path.read_text()
        assert len(code.splitlines()) <= 400, str(path)
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        package = module.rsplit(".", 1)[0]
        layer = package.split(".")[-1]
        for node in ast.walk(ast.parse(code)):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            target = node.module
            if node.level:
                target = importlib.util.resolve_name("." * node.level + target, package)
            assert not (
                layer in {"fetch", "parse", "image", "assemble", "utils"}
                and target in {"quire.cli", "quire.manga"}
            ), (module, target)
            assert not (
                layer in {"parse", "image", "utils"} and target.startswith("quire.fetch")
            ), (module, target)
            assert not (
                layer == "fetch"
                and target.startswith(("quire.parse", "quire.image", "quire.assemble"))
            ), (module, target)
