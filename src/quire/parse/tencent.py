"""Decode the public reader's DATA/nonce envelope (ac.qq.com reader algorithm)."""

from __future__ import annotations

import base64
import json
import re


def reader_data(html: str) -> dict[str, object] | None:
    data = re.search(r"\bvar\s+DATA\s*=\s*['\"]([A-Za-z0-9+/=]+)['\"]", html)
    nonce = re.search(
        r"""window\s*\[\s*["']no["']\s*\+\s*["']nce["']\s*\]\s*=\s*['"]['"]\s*\+\s*['"]([a-zA-Z0-9]+)['"]""",
        html,
    )
    mixed_matches = re.findall(
        r'window\["(?:n|no|non|nonc)"\s*\+\s*"(?:once|nce|ce|e)"\]\s*=\s*(.+?);', html
    )
    mixed = [value for value in mixed_matches if "eval" in value]

    nonce_text = nonce[1] if nonce else ""
    if mixed:
        expression = mixed[-1]

        def number(match: re.Match[str]) -> str:
            import ast

            source = match[1].replace("document.children", "1").replace("window.Array", "1")
            source = re.sub(
                r"'(\d+)'\.substring\((\d+)\)", lambda m: m[1][int(m[2]) :] or "0", source
            )
            source = re.sub(r"'(.)'\.charCodeAt\(\)", lambda m: str(ord(m[1])), source)
            source = re.sub(
                r"(!+)(\d+)",
                lambda m: str(int(bool(int(m[2]))) if len(m[1]) % 2 == 0 else int(not int(m[2]))),
                source,
            )
            if len(source) > 160:
                raise ValueError("Expression too long")
            source = re.sub(
                r"Math.pow\((\d{1,2}),(\d)\)", lambda m: str(int(m[1]) ** int(m[2])), source
            )
            source = re.sub(
                r"Math.round\((\d*\.?\d+)\)", lambda m: str(int(float(m[1]) + 0.5)), source
            )
            source = re.sub(r"~~(\d*\.?\d+)", lambda m: str(int(float(m[1]))), source)
            source = re.sub(r"!!(\d+)", lambda m: str(int(bool(int(m[1])))), source)
            source = re.sub(r"!(\d+)", lambda m: str(int(not int(m[1]))), source)
            if re.fullmatch(r"!\d+", source):
                return str(int(not int(source[1:])))
            ternary = re.fullmatch(r"(\d+)\s*(<=|>=|<|>|==)\s*(\d+)\?(\d+):(\d+)", source)
            if ternary:
                a, op, b, yes, no = ternary.groups()
                truth = {
                    "<=": int(a) <= int(b),
                    ">=": int(a) >= int(b),
                    "<": int(a) < int(b),
                    ">": int(a) > int(b),
                    "==": a == b,
                }[op]
                return yes if truth else no
            import operator

            operations = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.BitAnd: operator.and_,
                ast.BitOr: operator.or_,
                ast.BitXor: operator.xor,
            }

            def value(node: ast.AST) -> int:
                if (
                    isinstance(node, ast.Constant)
                    and type(node.value) is int
                    and abs(node.value) < 1000000
                ):
                    return node.value
                if isinstance(node, ast.BinOp) and type(node.op) in operations:
                    result = operations[type(node.op)](value(node.left), value(node.right))
                    if abs(result) < 1000000:
                        return int(result)
                raise ValueError("Unsupported nonce expression")

            return str(value(ast.parse(source, mode="eval").body))

        try:
            expression = re.sub(
                r'\(\+eval\("([^"\n]+)"\)\)\.toString\(\)',
                lambda m: '"' + number(m) + '"',
                expression,
            )
            if not re.fullmatch(r'\s*"[a-zA-Z0-9]*"(?:\s*\+\s*"[a-zA-Z0-9]*")*\s*', expression):
                return None
            nonce_text = "".join(re.findall(r'"([a-zA-Z0-9]*)"', expression))
        except (ValueError, SyntaxError):
            return None
    if not data or not nonce_text or len(data[1]) > 8_000_000:
        return None
    chars = list(data[1])
    for token in reversed(re.findall(r"\d+[a-zA-Z]+", nonce_text)):
        position = int(re.match(r"\d+", token)[0]) & 255  # type: ignore[index]
        count = len(re.sub(r"\d+", "", token))
        del chars[position : position + count]
    try:
        value = json.loads(base64.b64decode("".join(chars), validate=True))
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def extract_tencent_images(html: str) -> list[str]:
    data = reader_data(html)
    rows = data.get("picture") if data else None
    if not isinstance(rows, list) or len(rows) > 2000:
        return []
    return [
        row["url"]
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("url"), str)
        and row["url"].startswith(("https://", "http://"))
    ]
