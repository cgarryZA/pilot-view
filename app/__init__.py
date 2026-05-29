"""Pilot View application package.

Loads the project-root .env into the process environment at import time, BEFORE
any submodule (sources, auth, …) reads os.getenv. Uses setdefault so anything
already set in the real environment (e.g. systemd Environment=) takes priority;
.env only fills the gaps. This is what makes runtime source switches (which
persist to .env) actually survive a restart.
"""

import os
from pathlib import Path


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = env_file.read_text()
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


_load_dotenv()
