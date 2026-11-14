"""PostToolUse hook: lint, docstring-check and type-check every Python file Claude edits.

Claude Code pipes the tool call as JSON on stdin. If the edited file is a ``.py``
file, this runs ``ruff`` (style + docstrings + annotations) and ``mypy`` (types).
Exit code 2 sends the findings back to Claude so it fixes them before moving on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a command and return its exit code and combined output.

    Args:
        cmd: Command and arguments to execute.

    Returns:
        A ``(returncode, output)`` tuple.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _tool(name: str) -> list[str] | None:
    """Locate a checker, preferring ``uv run`` so the project venv is used.

    Args:
        name: Executable name, e.g. ``"ruff"``.

    Returns:
        The command prefix to invoke it, or ``None`` if unavailable.
    """
    if shutil.which("uv"):
        return ["uv", "run", "--quiet", name]
    if shutil.which(name):
        return [name]
    return None


def main() -> int:
    """Check the edited file and report problems to Claude.

    Returns:
        0 when clean or not a Python file, 2 when problems were found.
    """
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # Not something we understand; never block the session.

    file_path = payload.get("tool_input", {}).get("file_path", "")
    path = Path(file_path)
    # Only check project source files; skip notebooks, hooks and tests' fixtures.
    if path.suffix != ".py" or not path.exists() or ".claude" in path.parts:
        return 0

    problems: list[str] = []

    ruff = _tool("ruff")
    if ruff:
        # Auto-fix the trivial stuff (imports, spacing) first, then report the rest.
        _run([*ruff, "check", "--fix", "--quiet", str(path)])
        _run([*ruff, "format", "--quiet", str(path)])
        code, out = _run([*ruff, "check", str(path)])
        if code != 0:
            problems.append(f"ruff (style/docstrings/annotations):\n{out}")

    mypy = _tool("mypy")
    if mypy:
        code, out = _run([*mypy, str(path)])
        if code != 0:
            problems.append(f"mypy (type check):\n{out}")

    if problems:
        print(
            f"Quality check failed for {path.name}. Fix these before continuing:\n\n"
            + "\n\n".join(problems),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
