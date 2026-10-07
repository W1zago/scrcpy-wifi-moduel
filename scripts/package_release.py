#!/usr/bin/env python3
"""package_release.py — зібрати релізний архів для Windows або Linux.

Використовується і в GitHub Actions, і вручну:
    python scripts/package_release.py --platform win --out dist/scrcpy-wifi-module-win64.zip
    python scripts/package_release.py --platform linux --out dist/scrcpy-wifi-module-linux.tar.gz

В архів входять: лаунчери під ОС, tools/, ui/ (БЕЗ node_modules — ставиться
командою cd ui && npm install), scripts/, src/, CMakeLists.txt, README.md,
LICENSE і зібрані бінарники з build/ (якщо є).
"""
import argparse
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WIN_LAUNCHERS = ["START.bat", "START_CONSOLE.bat", "START_UI.bat"]
LINUX_LAUNCHERS = ["START.sh", "START_CONSOLE.sh", "START_UI.sh"]
ALWAYS = ["tools", "ui", "scripts", "src", "CMakeLists.txt", "README.md",
          "LICENSE", "ARCHITECTURE.md", "run_simulation.py"]

SKIP_DIRS = {"__pycache__", "node_modules", ".git"}
SKIP_SUFFIXES = (".pyc",)


def collect(platform):
    files = []
    for name in (WIN_LAUNCHERS if platform == "win" else LINUX_LAUNCHERS):
        p = ROOT / name
        if p.is_file():
            files.append(p)
    for name in ALWAYS:
        p = ROOT / name
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if any(part in SKIP_DIRS for part in f.relative_to(ROOT).parts):
                    continue
                if f.suffix in SKIP_SUFFIXES:
                    continue
                files.append(f)
    # Зібрані бінарники agent_* (build/Release/*.exe на Windows, build/agent_* на Linux)
    for pat in ("agent_sender*", "agent_receiver*"):
        for f in sorted((ROOT / "build").rglob(pat)):
            if f.is_file() and f.suffix not in (".obj", ".pdb", ".ilk", ".exp", ".lib"):
                files.append(f)
    seen, unique = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["win", "linux"], required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    top = f"scrcpy-wifi-module-{args.platform}"
    files = collect(args.platform)
    if not files:
        print("Nothing to pack!", file=sys.stderr)
        return 1
    print(f"Packing {len(files)} files -> {out}")
    if args.platform == "win":
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f"{top}/{f.relative_to(ROOT).as_posix()}")
    else:
        with tarfile.open(out, "w:gz") as t:
            for f in files:
                t.add(f, f"{top}/{f.relative_to(ROOT).as_posix()}")
    print(f"OK: {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
