"""Move the application into the ``app/`` package and rewrite imports.

Idempotent: a second run changes nothing. Branches cut before the move can
rebase onto main and re-run this to carry their own new files and imports
across, then run the tests.

    python scripts/move_to_package.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "app"

PACKAGES = (
    "routes",
    "middleware",
    "dependencies",
    "services",
    "repositories",
    "schemas",
    "infrastructure",
    "shared",
    "workers",
)
FILE_MODULES = ("config", "errors", "main")
OLD_MODULES = PACKAGES + FILE_MODULES

MOVES = [
    ("main.py", "app/main.py"),
    ("app.py", "app/factory.py"),
    ("config.py", "app/config.py"),
    ("errors.py", "app/errors.py"),
    ("config/apps.yaml", "app/data/apps.yaml"),
    *((name, f"app/{name}") for name in PACKAGES),
    ("data", "app/data"),
    ("static", "app/static"),
    ("templates", "app/templates"),
]

SCAN_DIRS = ("app", "tests", "scripts")
SELF = Path(__file__).resolve()

_MOD = "|".join(OLD_MODULES)
FROM_RE = re.compile(rf"^(\s*)from ({_MOD})((?:\.\w+)*) import\b", re.M)
IMPORT_DOTTED_RE = re.compile(rf"^(\s*)import ({_MOD})((?:\.\w+)+)(\s+as\s+\w+)", re.M)
IMPORT_BARE_RE = re.compile(rf"^(\s*)import ({_MOD})[ \t]*$", re.M)
IMPORT_UNALIASED_RE = re.compile(rf"^\s*import ({_MOD})\.\w+[\w.]*[ \t]*$", re.M)
FROM_APP_RE = re.compile(r"^(\s*)from app import (\(?)([^\n]*)", re.M)
IMPORT_APP_RE = re.compile(r"^\s*import app[ \t]*$", re.M)
STRING_RE = re.compile(r"""(["'])((?:\w+)(?:\.\w+)+(?::\w+)?)\1""")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    )


def is_tracked(path: Path) -> bool:
    out = git("ls-files", "--", str(path.relative_to(ROOT))).stdout
    return bool(out.strip())


def move_file(src: Path, dst: Path, moved: list[str], problems: list[str]) -> None:
    if dst.exists():
        problems.append(
            f"both exist, left alone: {src.relative_to(ROOT)} and {dst.relative_to(ROOT)}"
        )
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_tracked(src):
        git("mv", str(src.relative_to(ROOT)), str(dst.relative_to(ROOT)))
    else:
        src.rename(dst)
    moved.append(f"{src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")


def remove_empty_dirs(path: Path) -> None:
    for sub in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if sub.is_dir() and sub.name == "__pycache__":
            shutil.rmtree(sub)
        elif sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()
    if path.is_dir() and not any(path.iterdir()):
        path.rmdir()


def move_paths() -> tuple[list[str], list[str]]:
    moved: list[str] = []
    problems: list[str] = []
    PKG.mkdir(exist_ok=True)
    init = PKG / "__init__.py"
    if not init.exists():
        init.touch()
        git("add", "app/__init__.py")
        moved.append("created app/__init__.py")

    for old, new in MOVES:
        src, dst = ROOT / old, ROOT / new
        if not src.exists():
            continue
        if src.is_file():
            move_file(src, dst, moved, problems)
            continue
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            git("mv", old, new)
            moved.append(f"{old}/ -> {new}/")
            continue
        for path in sorted(src.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                move_file(path, dst / path.relative_to(src), moved, problems)
        remove_empty_dirs(src)

    if (ROOT / "config").is_dir():
        remove_empty_dirs(ROOT / "config")
    return moved, problems


def defined_names(module_file: Path) -> set[str]:
    if not module_file.is_file():
        return set()
    text = module_file.read_text()
    names = set(re.findall(r"^(?:async\s+)?(?:def|class)\s+(\w+)", text, re.M))
    names |= set(re.findall(r"^(\w+)\s*[:=]", text, re.M))
    for imported in re.findall(
        r"^\s*(?:from\s+\S+\s+)?import\s+\(?([^)\n]*)", text, re.M
    ):
        for part in imported.split(","):
            bits = part.split()
            if bits:
                names.add(bits[-1])
    for block in re.findall(r"^\s*from\s+\S+\s+import\s+\(([^)]*)\)", text, re.M):
        for part in block.split(","):
            bits = part.split()
            if bits:
                names.add(bits[-1])
    return names


def package_children() -> set[str]:
    return {p.stem for p in PKG.iterdir() if p.suffix == ".py" or p.is_dir()} - {
        "__init__",
        "__pycache__",
    }


def rewrite_string(
    match: re.Match[str], file_names: dict[str, set[str]], children: set[str]
) -> str:
    quote, value = match.group(1), match.group(2)
    dotted, _, attr = value.partition(":")
    head, second = dotted.split(".")[:2]
    if head in PACKAGES:
        target = PKG / head / second
        if not (target.is_dir() or target.with_suffix(".py").is_file()):
            return match.group(0)
        new = f"app.{dotted}"
    elif head in FILE_MODULES:
        if second not in file_names[head]:
            return match.group(0)
        new = f"app.{dotted}"
    elif head == "app":
        if second in children or second not in file_names["factory"]:
            return match.group(0)
        new = "app.factory." + dotted.split(".", 1)[1]
    else:
        return match.group(0)
    return f"{quote}{new}{':' + attr if attr else ''}{quote}"


def rewrite_from_app(
    match: re.Match[str], children: set[str], problems: list[str], rel: str
) -> str:
    indent, paren, rest = match.groups()
    first = re.split(r"[\s,()#]+", rest.strip())[0] if rest.strip() else ""
    if not first:
        problems.append(f"{rel}: multi-line 'from app import (' needs a manual look")
        return match.group(0)
    if first in children:
        return match.group(0)
    return f"{indent}from app.factory import {paren}{rest}"


def rewrite_source(
    text: str,
    rel: str,
    file_names: dict[str, set[str]],
    children: set[str],
    problems: list[str],
) -> str:
    text = FROM_APP_RE.sub(lambda m: rewrite_from_app(m, children, problems, rel), text)
    text = FROM_RE.sub(
        lambda m: f"{m.group(1)}from app.{m.group(2)}{m.group(3)} import", text
    )
    text = IMPORT_DOTTED_RE.sub(
        lambda m: f"{m.group(1)}import app.{m.group(2)}{m.group(3)}{m.group(4)}", text
    )
    text = IMPORT_BARE_RE.sub(
        lambda m: f"{m.group(1)}from app import {m.group(2)}", text
    )
    text = STRING_RE.sub(lambda m: rewrite_string(m, file_names, children), text)
    for m in IMPORT_UNALIASED_RE.finditer(text):
        problems.append(f"{rel}: unaliased '{m.group(0).strip()}' needs a manual look")
    if IMPORT_APP_RE.search(text):
        problems.append(f"{rel}: bare 'import app' needs a manual look")
    return text


def rewrite_imports() -> tuple[list[str], list[str]]:
    problems: list[str] = []
    changed: list[str] = []
    file_names = {name: defined_names(PKG / f"{name}.py") for name in FILE_MODULES}
    file_names["factory"] = defined_names(PKG / "factory.py")
    children = package_children()
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            if path.resolve() == SELF or "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(ROOT))
            old = path.read_text()
            new = rewrite_source(old, rel, file_names, children, problems)
            if new != old:
                path.write_text(new)
                changed.append(rel)
    return changed, problems


def run_ruff(files: list[str]) -> str:
    if not files:
        return "ruff: nothing to fix"
    venv_ruff = Path(sys.executable).with_name("ruff")
    ruff = str(venv_ruff) if venv_ruff.is_file() else shutil.which("ruff") or ""
    if not Path(ruff).is_file():
        return "ruff not found: run `ruff check --select I --fix` and `ruff format`"
    subprocess.run(
        [ruff, "check", "--select", "I", "--fix", "--quiet", *files],
        cwd=ROOT,
        check=False,
    )
    subprocess.run([ruff, "format", "--quiet", *files], cwd=ROOT, check=False)
    return f"ruff: sorted imports and formatted {len(files)} file(s)"


def main() -> int:
    moved, move_problems = move_paths()
    changed, import_problems = rewrite_imports()
    print(f"moved {len(moved)} path(s)")
    for line in moved:
        print(f"  {line}")
    print(f"rewrote imports or module strings in {len(changed)} file(s)")
    for line in changed:
        print(f"  {line}")
    print(run_ruff(changed))
    problems = move_problems + import_problems
    for line in problems:
        print(f"CHECK: {line}")
    if not moved and not changed:
        print("nothing to do")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
