from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"

if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    raise SystemExit(
        subprocess.call(
            [str(VENV_PYTHON), "-m", "shopper_merge_bot", *sys.argv[1:]],
            cwd=str(PROJECT_DIR),
        )
    )

from shopper_merge_bot.runtime import main  # noqa: E402


if __name__ == "__main__":
    main()
