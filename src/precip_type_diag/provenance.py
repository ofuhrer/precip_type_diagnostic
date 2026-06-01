"""Runtime provenance for operational outputs."""

from __future__ import annotations

import platform
import subprocess
import sys
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

DEPENDENCY_DISTRIBUTIONS = (
    "precip-type-diag",
    "numpy",
    "numba",
    "earthkit-data",
    "eccodes",
    "eccodes-cosmo-resources-python",
)


def _distribution_versions(names: Iterable[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _git_output(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return result.stdout.strip()


def _git_provenance(repo_root: Path) -> dict[str, object]:
    commit = _git_output(repo_root, "rev-parse", "HEAD")
    if not commit:
        return {"available": False}

    branch = _git_output(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git_output(repo_root, "status", "--porcelain")
    return {
        "available": True,
        "commit": commit,
        "branch": branch,
        "dirty": bool(status),
    }


def collect_runtime_provenance(repo_root: Path | None = None) -> dict[str, object]:
    """Collect reproducibility metadata for an operational run."""

    root = Path(__file__).resolve().parents[2] if repo_root is None else Path(repo_root)
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "package_versions": _distribution_versions(DEPENDENCY_DISTRIBUTIONS),
        "git": _git_provenance(root),
        "argv": list(sys.argv),
    }
