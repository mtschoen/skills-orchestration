"""Shared loader and factories for the agent-remote tests."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType


def load_agent_remote() -> ModuleType:
    """Load the hyphenated reference script as an importable module."""
    module_path = Path(__file__).parents[1] / "references" / "agent-remote.py"
    specification = importlib.util.spec_from_file_location("agent_remote", module_path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Could not load {module_path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


agent_remote = load_agent_remote()


def completed_process(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a subprocess result suitable for mocked SSH calls."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def run_arguments(**overrides: object) -> argparse.Namespace:
    """Build arguments accepted by cmd_run."""
    values: dict[str, object] = {
        "host": "worker@example",
        "repo_path": "/srv/project",
        "prompt": "run the checks",
        "branch": "agent-remote/checks",
        "permission_mode": "acceptEdits",
        "agent": "opencode",
        "model": None,
        "extra_allow": [],
        "os": "posix",
    }
    values.update(overrides)
    return argparse.Namespace(**values)
