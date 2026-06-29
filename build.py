"""Build a Windows release zip for Livestream Spotter."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from livestream_spotter import __version__


ROOT = Path(__file__).resolve().parent
SPEC_PATH = ROOT / "livestream-spotter.spec"
DIST_EXE = ROOT / "dist" / "livestream-spotter.exe"
RELEASE_ROOT = ROOT / "release"
RELEASE_DIR = RELEASE_ROOT / f"livestream-spotter-{__version__}"
ZIP_PATH = RELEASE_ROOT / f"livestream-spotter-{__version__}.zip"


def require_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit(
            "PyInstaller is not installed. Install it in this environment, then "
            "run: python build.py"
        )


def run_pyinstaller() -> None:
    require_pyinstaller()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            str(SPEC_PATH),
        ],
        cwd=ROOT,
        check=True,
    )
    if not DIST_EXE.exists():
        raise SystemExit(f"Expected built exe was not found: {DIST_EXE}")


def assemble_release() -> None:
    if RELEASE_DIR.exists():
        shutil.rmtree(RELEASE_DIR)
    RELEASE_DIR.mkdir(parents=True)
    shutil.copy2(DIST_EXE, RELEASE_DIR / "livestream-spotter.exe")
    # Ship the pristine template, never the working-tree config (which may carry
    # local edits such as a real OBS password or raw_dump_enabled = true).
    shutil.copy2(ROOT / "config.default.toml", RELEASE_DIR / "config.toml")
    shutil.copy2(ROOT / "README.txt", RELEASE_DIR / "README.txt")


def zip_release() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(RELEASE_DIR.rglob("*")):
            archive.write(path, path.relative_to(RELEASE_ROOT))


def main() -> int:
    run_pyinstaller()
    assemble_release()
    zip_release()
    print(f"Built {ZIP_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
