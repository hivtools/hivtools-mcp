"""Project name and version, read straight from ``pyproject.toml``.

``pyproject.toml`` is the single source of truth: bump ``[project].version`` in a
commit and cut the matching ``vX.Y.Z`` release (a CI guard fails the release if
they disagree). The project is not installed as a package, so ``importlib.metadata``
would not see it - we parse the file, which is present in the image (the Dockerfile
copies it to ``/code``).
"""

import sys
from functools import lru_cache
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # 3.10 - tomllib is not in the stdlib yet
    import tomli as tomllib

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@lru_cache(maxsize=1)
def _pyproject() -> dict:
    try:
        return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def get_version() -> str:
    return _pyproject().get("project", {}).get("version", "unknown")


def get_name() -> str:
    return _pyproject().get("project", {}).get("name", "hivtools-mcp")
