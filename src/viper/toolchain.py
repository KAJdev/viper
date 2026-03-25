"""manages python-build-standalone toolchains.

downloads and caches standalone cpython distributions used to compile
binaries. cached at ~/Library/Caches/viper/toolchain/ (macos),
~/.cache/viper/toolchain/ (linux).

the python version is a build-time parameter -- any version available in
the PBS release can be used (e.g. 3.12, 3.13, 3.14).
"""
from __future__ import annotations
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


DEFAULT_PYTHON = "3.14"

# python-build-standalone release tag (date-based)
PBS_RELEASE = "20260324"

PBS_BASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download"


def get_cache_dir() -> Path:
    """platform-appropriate cache directory for viper."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "viper"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "viper"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "viper"
    return base


def get_toolchain_dir(python_version: str) -> Path:
    """path to the cached toolchain for a given python minor version."""
    return get_cache_dir() / "toolchain" / f"cpython-{python_version}-{PBS_RELEASE}"


def ensure_toolchain(python_version: str = DEFAULT_PYTHON, verbose: bool = False) -> Path:
    """download python-build-standalone if not already cached.

    python_version: minor version like "3.14" or full like "3.14.3".
                    minor versions are resolved to the full version
                    available in the PBS release.
    returns: path to the toolchain installation directory.
    """
    minor = _to_minor(python_version)
    tc_dir = get_toolchain_dir(minor)
    marker = tc_dir / ".complete"

    if marker.exists():
        return tc_dir

    if verbose:
        print(f"downloading python {minor} toolchain...")

    tc_dir.mkdir(parents=True, exist_ok=True)

    full_version = _resolve_full_version(minor, verbose=verbose)
    archive_url = _build_download_url(full_version)

    if verbose:
        print(f"  {archive_url}")

    _download_and_extract(archive_url, tc_dir, verbose=verbose)
    marker.touch()

    if verbose:
        print(f"  cached at {tc_dir}")

    return tc_dir


# -- path helpers (all take python minor version) --

def get_python_bin(tc_dir: Path) -> Path:
    return tc_dir / "bin" / "python3"


def get_python_include(tc_dir: Path, python_version: str) -> Path:
    return tc_dir / "include" / f"python{python_version}"


def get_python_dylib(tc_dir: Path, python_version: str) -> Path:
    if sys.platform == "darwin":
        return tc_dir / "lib" / f"libpython{python_version}.dylib"
    else:
        return tc_dir / "lib" / f"libpython{python_version}.so"


def get_python_stdlib(tc_dir: Path, python_version: str) -> Path:
    return tc_dir / "lib" / f"python{python_version}"


# -- internal --

def _to_minor(version: str) -> str:
    """normalize to major.minor (e.g. "3.14.3" -> "3.14")."""
    parts = version.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return version


def _resolve_full_version(minor: str, verbose: bool = False) -> str:
    """resolve a minor version to the full cpython version in the PBS release.

    queries the github release assets list (cached to disk) and finds the
    matching cpython-{major}.{minor}.{patch} entry.
    """
    cache_file = get_cache_dir() / "pbs_versions" / f"{PBS_RELEASE}.json"

    versions: dict[str, str] | None = None
    if cache_file.exists():
        try:
            versions = json.loads(cache_file.read_text())
        except Exception:
            pass

    if versions and minor in versions:
        return versions[minor]

    if verbose:
        print(f"  resolving cpython {minor}.x for PBS release {PBS_RELEASE}...")

    triple = _get_platform_triple()
    api_url = (
        f"https://api.github.com/repos/astral-sh/python-build-standalone"
        f"/releases/tags/{PBS_RELEASE}"
    )

    # fetch asset list
    try:
        result = subprocess.run(
            ["curl", "-fsSL", api_url],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
    except Exception as e:
        raise RuntimeError(
            f"failed to query PBS release {PBS_RELEASE}: {e}"
        ) from e

    # build minor -> full version map from all install_only_stripped assets
    # for the current platform
    versions = {}
    suffix = f"-{triple}-install_only_stripped.tar.gz"
    for asset in data.get("assets", []):
        name = asset["name"]
        if not name.startswith("cpython-") or not name.endswith(suffix):
            continue
        # cpython-3.14.3+20260324-aarch64-apple-darwin-install_only_stripped.tar.gz
        ver_part = name.split("+")[0].replace("cpython-", "")  # "3.14.3"
        ver_minor = _to_minor(ver_part)
        versions[ver_minor] = ver_part

    # cache for future calls
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(versions))

    if minor not in versions:
        available = ", ".join(sorted(versions.keys())) or "(none)"
        raise RuntimeError(
            f"python {minor} not found in PBS release {PBS_RELEASE}. "
            f"available: {available}"
        )

    return versions[minor]


def _get_platform_triple() -> str:
    machine = platform.machine().lower()
    system = platform.system().lower()

    if system == "darwin":
        arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"{arch}-apple-darwin"
    elif system == "linux":
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu"
        elif machine in ("aarch64", "arm64"):
            return "aarch64-unknown-linux-gnu"
        else:
            raise RuntimeError(f"unsupported linux architecture: {machine}")
    else:
        raise RuntimeError(f"unsupported platform: {system}")


def _build_download_url(full_version: str) -> str:
    triple = _get_platform_triple()
    filename = f"cpython-{full_version}+{PBS_RELEASE}-{triple}-install_only_stripped.tar.gz"
    return f"{PBS_BASE_URL}/{PBS_RELEASE}/{filename}"


def _download_and_extract(url: str, dest: Path, verbose: bool = False) -> None:
    """download a tar.gz and extract it, stripping the top-level 'python/' prefix."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        try:
            cmd = ["curl", "-fSL",
                   "--progress-bar" if verbose else "--silent",
                   "-o", str(tmp_path), url]
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            import urllib.request
            if verbose:
                print("  (using urllib, curl not found)")
            urllib.request.urlretrieve(url, tmp_path)

        with tarfile.open(tmp_path, "r:gz") as tf:
            for member in tf.getmembers():
                # strip the "python/" prefix from the archive
                parts = member.name.split("/", 1)
                if len(parts) < 2 or parts[0] != "python":
                    continue
                member.name = parts[1]
                if not member.name:
                    continue
                tf.extract(member, dest, filter="data")
    finally:
        tmp_path.unlink(missing_ok=True)
