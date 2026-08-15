#!/usr/bin/env python3
"""Ukur ulang angka baseline refactor (REFACTOR_PLAN.md §1.1, §1.2, §5).

Jalankan dari akar repo:

    python tools/complexity_report.py              # tabel lengkap
    python tools/complexity_report.py --summary    # hanya metrik §5
    python tools/complexity_report.py --diff tools/baseline.txt

Tujuannya supaya progres tiap fase terukur, bukan terasa. Angka LOC file
memakai jumlah baris mentah; LOC fungsi memakai rentang `lineno..end_lineno`
(termasuk dekorator tidak dihitung, sesuai tabel di plan).

CX adalah perkiraan cyclomatic complexity ala mccabe: 1 + jumlah titik
percabangan. Ini pendekatan yang sama dengan aturan C901 ruff, jadi angkanya
bisa dipakai untuk menilai kapan sebuah baris `per-file-ignores` boleh dicabut.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "qBittorrent", ".ruff_cache"}
SCAN_ROOTS = ("bot", "web")

# Ambang dari REFACTOR_PLAN.md §2 (target ukuran) dan §5 (kriteria selesai).
FILE_LOC_LIMIT = 500
FUNC_LOC_BIG = 100
FUNC_LOC_LIMIT = 50


@dataclass
class FuncStat:
    loc: int
    cx: int
    path: str
    lineno: int
    qualname: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.lineno}"


@dataclass
class FileStat:
    loc: int
    path: str


class ComplexityVisitor(ast.NodeVisitor):
    """Hitung titik percabangan dalam satu fungsi, tanpa masuk fungsi bersarang."""

    def __init__(self) -> None:
        self.score = 1

    def _bump(self, node: ast.AST, amount: int = 1) -> None:
        self.score += amount
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._bump(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._bump(node)

    def visit_For(self, node: ast.For) -> None:
        self._bump(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._bump(node)

    def visit_While(self, node: ast.While) -> None:
        self._bump(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._bump(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._bump(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # `a and b and c` menyumbang 2 cabang, bukan 1.
        self._bump(node, len(node.values) - 1)

    def visit_match_case(self, node: ast.match_case) -> None:
        self._bump(node)

    def _comprehension(self, node: ast.AST) -> None:
        generators = getattr(node, "generators", [])
        extra = sum(1 + len(gen.ifs) for gen in generators)
        self._bump(node, extra)

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension

    def _skip_nested(self, node: ast.AST) -> None:
        """Fungsi bersarang punya CX sendiri, jangan dijumlahkan ke induknya."""

    visit_FunctionDef = _skip_nested
    visit_AsyncFunctionDef = _skip_nested
    visit_Lambda = _skip_nested


def complexity(node: ast.AST) -> int:
    visitor = ComplexityVisitor()
    for child in ast.iter_child_nodes(node):
        visitor.visit(child)
    return visitor.score


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for base in SCAN_ROOTS:
        start = root / base
        if not start.is_dir():
            continue
        for path in sorted(start.rglob("*.py")):
            if EXCLUDE_DIRS.isdisjoint(path.parts):
                files.append(path)
    return files


def collect_functions(tree: ast.AST, rel: str) -> list[FuncStat]:
    stats: list[FuncStat] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                end = child.end_lineno or child.lineno
                stats.append(
                    FuncStat(
                        loc=end - child.lineno + 1,
                        cx=complexity(child),
                        path=rel,
                        lineno=child.lineno,
                        qualname=f"{prefix}{child.name}",
                    )
                )
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return stats


def count_smells(root: Path, files: list[Path]) -> tuple[int, int]:
    """Hitung bare `except:` dan pemanggilan `eval()` (§1.4)."""
    bare_except = 0
    eval_calls = 0
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                bare_except += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "eval"
            ):
                eval_calls += 1
    return bare_except, eval_calls


def gather(root: Path) -> tuple[list[FileStat], list[FuncStat], int, int]:
    files = iter_python_files(root)
    file_stats: list[FileStat] = []
    func_stats: list[FuncStat] = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        file_stats.append(FileStat(loc=len(source.splitlines()), path=rel))
        try:
            tree = ast.parse(source)
        except SyntaxError as err:
            print(f"lewati {rel}: {err}", file=sys.stderr)
            continue
        func_stats.extend(collect_functions(tree, rel))
    bare_except, eval_calls = count_smells(root, files)
    return file_stats, func_stats, bare_except, eval_calls


def render_tables(files: list[FileStat], funcs: list[FuncStat], limit: int) -> str:
    lines = ["## File terbesar", "", "| LOC | File |", "|-----|------|"]
    big_files = sorted(files, key=lambda f: -f.loc)
    for stat in big_files[:limit]:
        lines.append(f"| {stat.loc} | `{stat.path}` |")

    lines += [
        "",
        "## Fungsi terbesar",
        "",
        "| LOC | CX | Lokasi | Fungsi |",
        "|-----|----|--------|--------|",
    ]
    for stat in sorted(funcs, key=lambda f: -f.loc)[:limit]:
        lines.append(
            f"| {stat.loc} | {stat.cx} | `{stat.location}` | `{stat.qualname}()` |"
        )
    return "\n".join(lines)


def metrics(
    files: list[FileStat], funcs: list[FuncStat], bare_except: int, eval_calls: int
) -> dict[str, int]:
    return {
        "file_total": len(files),
        "file_gt_500_loc": sum(1 for f in files if f.loc > FILE_LOC_LIMIT),
        "func_total": len(funcs),
        "func_gt_100_loc": sum(1 for f in funcs if f.loc > FUNC_LOC_BIG),
        "func_gt_50_loc": sum(1 for f in funcs if f.loc > FUNC_LOC_LIMIT),
        "cx_max": max((f.cx for f in funcs), default=0),
        "func_cx_gt_10": sum(1 for f in funcs if f.cx > 10),
        "bare_except": bare_except,
        "eval_calls": eval_calls,
    }


def render_summary(values: dict[str, int]) -> str:
    width = max(len(k) for k in values)
    return "\n".join(f"{k.ljust(width)} = {v}" for k, v in values.items())


def parse_summary(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key, raw = key.strip(), raw.strip()
        if key and raw.isdigit():
            values[key] = int(raw)
    return values


def render_diff(current: dict[str, int], previous: dict[str, int]) -> str:
    width = max(len(k) for k in current)
    lines = [f"{'metrik'.ljust(width)}   baseline  sekarang  delta", ""]
    for key, now in current.items():
        before = previous.get(key)
        if before is None:
            lines.append(f"{key.ljust(width)}   {'-':>8}  {now:>8}  {'baru':>6}")
            continue
        delta = now - before
        mark = "=" if delta == 0 else f"{delta:+d}"
        lines.append(f"{key.ljust(width)}   {before:>8}  {now:>8}  {mark:>6}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--limit", type=int, default=15, help="baris per tabel")
    parser.add_argument("--summary", action="store_true", help="hanya metrik §5")
    parser.add_argument("--diff", type=Path, help="bandingkan dengan file baseline")
    args = parser.parse_args()

    files, funcs, bare_except, eval_calls = gather(args.root)
    values = metrics(files, funcs, bare_except, eval_calls)

    if args.diff:
        previous = parse_summary(args.diff.read_text(encoding="utf-8"))
        print(render_diff(values, previous))
        return 0

    if args.summary:
        print(render_summary(values))
        return 0

    print(render_tables(files, funcs, args.limit))
    print("\n## Metrik\n")
    print(render_summary(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
