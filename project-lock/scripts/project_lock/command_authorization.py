"""Recognize ownership-proven release commands without executing shell input."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from .core import governing_lock


def authorizes_release(command: str, cwd: str, trusted_script: Path) -> bool:
    """Let the CLI recheck an exact release token under its mutation guard.

    This deliberately accepts only a simple invocation, not shell programs.
    Reject shell expansion even within quotes because quoting differs by shell.
    """
    if re.search(r"[;&|<>\r\n`$(){}^%*?\[\]~]", command):
        return False
    try:
        words = shlex.split(command, posix=False)
    except ValueError:
        return False
    words = [
        word[1:-1]
        if len(word) >= 2
        and (
            (word.startswith('"') and word.endswith('"'))
            or (word.startswith("'") and word.endswith("'"))
        )
        else word
        for word in words
    ]
    if len(words) != 6:
        return False
    interpreter, script, operation, target, option, token = words
    if operation != "release" or option != "--lock-id":
        return False
    if (
        interpreter not in {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}
        and (Path(cwd) / interpreter).resolve() != Path(sys.executable).resolve()
    ):
        return False
    if (Path(cwd) / script).resolve() != trusted_script.resolve():
        return False
    governed = governing_lock(Path(cwd) / target)
    return bool(governed and governed["lock"] and governed["lock"].get("lock_id") == token)
