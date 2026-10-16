#!/usr/bin/env python3
"""Render an HTML file to a PNG via headless Chrome.

Locates Chrome via the CHROME_PATH env var (override) or a short list
of OS-specific candidate paths. Uses --virtual-time-budget so Chart.js
animations complete before the screenshot fires.

Usage:
    python capture-screenshot.py <input.html> <output.png>
        [--width 880] [--height 720] [--scale 2] [--budget-ms 5000]

Default dimensions produce a 1760x1440 PNG (2x retina at 880x720
logical), matching the existing screenshot.png convention.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

CHROME_CANDIDATES = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def locate_chrome() -> str:
    override = os.environ.get("CHROME_PATH")
    if override:
        if not Path(override).is_file():
            sys.exit(f"error: CHROME_PATH={override!r} does not exist")
        return override
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    sys.exit(
        "error: could not locate Chrome. Set CHROME_PATH or install Chrome "
        f"to one of: {CHROME_CANDIDATES}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input_html", help="Path to the HTML file to render")
    parser.add_argument(
        "output_png", help="Path to write the PNG (absolute or relative)"
    )
    parser.add_argument(
        "--width", type=int, default=880, help="Logical viewport width (default: 880)"
    )
    parser.add_argument(
        "--height", type=int, default=720, help="Logical viewport height (default: 720)"
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=2,
        help="Device scale factor (default: 2 for retina)",
    )
    parser.add_argument(
        "--budget-ms",
        type=int,
        default=5000,
        help="Virtual time budget in ms (default: 5000)",
    )
    arguments = parser.parse_args()

    input_path = Path(arguments.input_html).resolve()
    if not input_path.is_file():
        sys.exit(f"error: input HTML not found: {input_path}")
    output_path = Path(arguments.output_png).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chrome = locate_chrome()
    url = input_path.as_uri()  # produces file:///... on Windows + POSIX

    command = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={arguments.width},{arguments.height}",
        f"--force-device-scale-factor={arguments.scale}",
        f"--virtual-time-budget={arguments.budget_ms}",
        f"--screenshot={output_path}",
        url,
    ]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(f"error: chrome exited {result.returncode}")
    if not output_path.is_file():
        sys.exit(f"error: chrome reported success but {output_path} not written")
    print(f"wrote {output_path} ({output_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
