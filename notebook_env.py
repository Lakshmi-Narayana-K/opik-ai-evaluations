"""Jupyter bootstrap: load opik-ai-evaluations/.venv packages reliably."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def load_env_file(override: bool = False) -> Path | None:
    """Load variables from ``REPO_ROOT/.env`` into ``os.environ``."""
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return None
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=override)
    except ImportError:
        _load_env_file_simple(env_path, override=override)
    return env_path


def _load_env_file_simple(env_path: Path, override: bool = False) -> None:
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = value


def activate() -> Path:
    """Put this repo's .venv site-packages on sys.path and return REPO_ROOT."""
    venv_root = REPO_ROOT / ".venv"
    py = venv_root / "bin" / "python"
    if not py.exists():
        raise FileNotFoundError(
            "Missing .venv. Run in terminal:\n"
            f"  cd {REPO_ROOT} && python3 -m venv .venv && "
            ".venv/bin/pip install -U pip opik pandas openai ipykernel && "
            '.venv/bin/python -m ipykernel install --user --name=opik-eval '
            '--display-name="Python (opik-eval)"'
        )

    site_packages = (
        venv_root
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if not site_packages.is_dir():
        raise FileNotFoundError(f"Missing venv site-packages at {site_packages}")

    site_str = str(site_packages.resolve())
    if site_str not in sys.path:
        sys.path.insert(0, site_str)

    if Path(sys.prefix).resolve() != venv_root.resolve():
        print(
            "Note: Jupyter is not using the .venv interpreter "
            f"(prefix={sys.prefix}).\n"
            f"Loaded packages from {site_packages}"
        )

    env_file = load_env_file()
    if env_file:
        print(f"Loaded environment from {env_file}")

    return REPO_ROOT
