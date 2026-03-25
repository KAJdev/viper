"""resolves and installs target package dependencies using uv or pip.

creates an isolated install directory with all deps available for freezing.
"""
from __future__ import annotations
import importlib
import subprocess
import sys
from pathlib import Path


def install_deps(
    deps: list[str],
    target_dir: Path,
    tc_python: Path | None = None,
    verbose: bool = False,
) -> None:
    """install dependencies into a target directory.

    uses uv if available, falls back to pip. the target directory is
    added to sys.path so the freezer can discover installed packages.

    tc_python: path to the toolchain python binary. when provided, uv
    installs packages compatible with that python version/abi.
    """
    if not deps:
        return

    target_dir.mkdir(parents=True, exist_ok=True)

    installer = _find_installer()
    if verbose:
        print(f"installing {len(deps)} dependencies ({installer})...")

    if installer == "uv":
        cmd = ["uv", "pip", "install", "--target", str(target_dir), "--quiet"]
        if tc_python:
            cmd += ["--python", str(tc_python)]
    else:
        python = str(tc_python) if tc_python else sys.executable
        cmd = [python, "-m", "pip", "install", "--target", str(target_dir), "--quiet"]

    cmd.extend(deps)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if verbose and result.stderr:
            for line in result.stderr.strip().splitlines()[:10]:
                print(f"  {line}", file=sys.stderr)
        # don't fail hard - some deps might be optional
        if verbose:
            print(f"  warning: {installer} exited {result.returncode}", file=sys.stderr)

    # make installed packages discoverable
    if str(target_dir) not in sys.path:
        sys.path.insert(0, str(target_dir))
    importlib.invalidate_caches()


def _find_installer() -> str:
    try:
        subprocess.run(["uv", "--version"], capture_output=True, check=True)
        return "uv"
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "pip"
