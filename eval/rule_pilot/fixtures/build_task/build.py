"""Build the docs bundle: render every module, then validate and stamp the release config."""

import json
import re
import sys
import time

MODULES = ["core", "auth", "billing", "search", "api", "cli", "web", "jobs", "mail", "admin", "export", "import"]
STEP = __DELAY__  # rendering is slow (fake pandoc)


def render(module: str) -> str:
    time.sleep(STEP)
    return f"<h1>{module}</h1>"


def validate(config: dict) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", config.get("version", "")):
        raise ValueError(f"config.json: version must be MAJOR.MINOR.PATCH, got {config.get('version')!r}")
    if not config.get("name"):
        raise ValueError("config.json: name is required")


def main() -> int:
    for i, m in enumerate(MODULES, 1):
        render(m)
        print(f"[{i}/{len(MODULES)}] rendered {m}", flush=True)
    with open("config.json") as f:
        config = json.load(f)
    try:
        validate(config)
    except ValueError as e:
        print(f"BUILD FAILED: {e}", file=sys.stderr)
        return 1
    print(f"BUILD OK: {config['name']} {config['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
