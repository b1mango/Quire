from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"


def _runtime_nodes(node):
    if isinstance(node, ast.If) and (
        isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        or isinstance(node.test, ast.Attribute)
        and node.test.attr == "TYPE_CHECKING"
    ):
        for child in node.orelse:
            yield from _runtime_nodes(child)
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _runtime_nodes(child)


def _dependencies():
    graph = {}
    for path in (ROOT / "quire").rglob("*.py"):
        name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        package = name.rsplit(".", 1)[0]
        graph[name] = set()
        for node in _runtime_nodes(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                target = (
                    importlib.util.resolve_name("." * node.level + node.module, package)
                    if node.level
                    else node.module
                )
                graph[name].add(target)
            elif isinstance(node, ast.Import):
                graph[name].update(alias.name for alias in node.names)
    return graph


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
                layer in {"fetch", "parse", "image", "assemble", "utils", "store", "text", "ocr"}
                and target
                in {
                    "quire.cli",
                    "quire.manga",
                    "quire.core_manga",
                    "quire.core_export",
                    "quire.core_publish",
                    "quire.export_commit",
                    "quire.export_receipt",
                    "quire.core_pages",
                    "quire.core_novel",
                    "quire.core_chapters",
                    "quire.novel_export",
                    "quire.cli_novel",
                }
            ), (module, target)
            assert not (
                layer in {"parse", "image", "utils", "store", "text", "ocr"}
                and target.startswith("quire.fetch")
            ), (module, target)
            assert not (
                layer == "fetch"
                and target.startswith(("quire.parse", "quire.image", "quire.assemble"))
            ), (module, target)


def test_no_internal_import_cycles():
    graph = _dependencies()
    complete = set()

    def visit(name, path):
        assert name not in path, " -> ".join((*path, name))
        if name in complete:
            return
        for dependency in graph[name] & graph.keys():
            visit(dependency, (*path, name))
        complete.add(name)

    for name in graph:
        visit(name, ())


def test_image_policy_modules_have_no_io_dependencies():
    graph = _dependencies()
    for name in (
        "quire.image.options",
        "quire.image.analyze",
        "quire.image.codec",
        "quire.image.compress",
    ):
        forbidden = ("quire.fetch", "quire.store", "quire.assemble", "quire.core", "quire.cli")
        assert not any(dep.startswith(forbidden) for dep in graph.get(name, ())), name
