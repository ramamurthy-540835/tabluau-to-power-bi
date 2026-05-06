"""Convenience launcher — run from project root: python ui/web/run.py"""
import subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).parent.parent.parent
subprocess.run(
    [sys.executable, "-m", "uvicorn", "ui.web.main:app",
     "--host", "0.0.0.0", "--port", "8000", "--reload"],
    cwd=str(ROOT),
)
