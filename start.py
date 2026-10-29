"""Bootstrap a checkout with standard-library Python, then start local services."""
import os
import subprocess
import sys
from pathlib import Path


def main():
    if sys.version_info < (3, 10):
        print("Python 3.10+ is required.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent
    interpreter = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        if not interpreter.exists():
            subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
        check = subprocess.run([str(interpreter), "-c", "import troubleshoot.launcher, fastapi, sqlalchemy, httpx, uvicorn"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode:
            subprocess.run([str(interpreter), "-m", "pip", "install", "-c", "constraints.txt", "-e", "."], cwd=root, check=True)
    except (OSError, subprocess.CalledProcessError):
        print("Setup failed. On Ubuntu install python3-venv (python3.10-venv on Ubuntu 22.04); if downloads fail, check your approved package proxy.", file=sys.stderr)
        return 1
    arguments = [str(interpreter), "-m", "troubleshoot.launcher", *sys.argv[1:]]
    if os.name != "nt":
        os.chdir(root)
        os.execv(str(interpreter), arguments)
    return subprocess.call(arguments, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
