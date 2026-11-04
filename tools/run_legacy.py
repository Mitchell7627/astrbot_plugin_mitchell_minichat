"""Run recovered historical regressions in a separate process (AstrBot is stubbed)."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    directory = root / "legacy_tests"
    env = {**os.environ, "MINICHAT_REPO": str(root), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-m", "pytest", str(directory), "-c", str(directory / "pytest.ini"),
               "-q", "--tb=short"]
    if args.junitxml:
        command.append("--junitxml=" + str(args.junitxml.resolve()))
    raise SystemExit(subprocess.call(command, cwd=root, env=env))


if __name__ == "__main__":
    main()
