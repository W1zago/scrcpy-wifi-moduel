#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""updater.py — явне оновлення програми з GitHub Releases.

Викликається ТІЛЬКИ за явною командою користувача:
    START.bat update            (Windows)
    ./START.sh update           (Linux)
    python tools/auto_run.py --update

Жодних фонових перевірок при звичайному старті тут немає.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"

# TODO: замініть на свій репозиторій, напр. "user/scrcpy-wifi-module"
REPO = os.environ.get("SCRCPY_WIFI_REPO", "W1zago/scrcpy-wifi-moduel")
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"

ASSET_HINTS = {
    "nt": ("win64.zip", ".zip"),
    "posix": ("linux.tar.gz", ".tar.gz"),
}


def current_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "scrcpy-wifi-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_latest():
    """Повертає (tag, asset_url, asset_name) або кидає виняток з людським текстом."""
    data = _http_get_json(API_LATEST)
    tag = (data.get("tag_name") or "").strip().lstrip("v")
    assets = data.get("assets") or []
    hint, ext = ASSET_HINTS.get(os.name, ("win64.zip", ".zip"))
    best = None
    for a in assets:
        name = a.get("name", "")
        if hint in name:
            best = a
            break
    if best is None:
        for a in assets:
            if a.get("name", "").endswith(ext):
                best = a
                break
    if not tag or best is None:
        raise RuntimeError("Release found, but no suitable asset for this OS.")
    return tag, best["browser_download_url"], best["name"]


def _ver_tuple(v):
    parts = []
    for p in v.strip().lstrip("v").split("."):
        num = "".join(c for c in p if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(latest, current):
    try:
        return _ver_tuple(latest) > _ver_tuple(current)
    except Exception:
        return latest != current


def download(url, dest, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "scrcpy-wifi-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 256)
    return dest


def _top_level_dir(members):
    """Зняти спільний top-level префікс архіву (scrcpy-wifi-module-win/...)."""
    if not members:
        return ""
    first = members[0].split("/")[0]
    if first and all(m == first or m.startswith(first + "/") for m in members):
        return first + "/"
    return ""


def stage_archive(archive, staging):
    staging = Path(staging)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            prefix = _top_level_dir(names)
            for m in names:
                rel = m[len(prefix):] if prefix and m.startswith(prefix) else m
                if not rel or rel.endswith("/"):
                    continue
                (staging / rel).parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(staging / rel, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 256)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            members = [m for m in t.getmembers() if m.isfile()]
            names = [m.name for m in members]
            prefix = _top_level_dir(names)
            for m in members:
                rel = m.name[len(prefix):] if prefix and m.name.startswith(prefix) else m.name
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                src = t.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 256)
    else:
        raise RuntimeError("Unknown archive format.")
    return staging


def write_apply_script(staging, new_version):
    """Генерує apply-скрипт, який застосовується ПІСЛЯ виходу Python."""
    staging = str(staging)
    if os.name == "nt":
        script = ROOT / "apply_update.bat"
        backup = f"backup_v{current_version()}"
        script.write_text(
            "@echo off\r\n"
            "cd /d \"%~dp0\"\r\n"
            f"if exist \"{backup}\" rmdir /S /Q \"{backup}\"\r\n"
            f"mkdir \"{backup}\"\r\n"
            "xcopy \"tools\" \"" + backup + "\\tools\\\" /E /I /Y >nul\r\n"
            "copy /Y VERSION \"" + backup + "\\\" >nul\r\n"
            f"robocopy \"{staging}\" \".\" /E /NFL /NDL /NJH /NJS\r\n"
            f"echo {new_version} > VERSION\r\n"
            f"rmdir /S /Q \"{staging}\"\r\n"
            "del \"%~f0\"\r\n"
            "echo [Update] Done. Restart START.bat\r\n",
            encoding="utf-8")
    else:
        script = ROOT / "apply_update.sh"
        backup = f"backup_v{current_version()}"
        script.write_text(
            "#!/usr/bin/env bash\nset -u\ncd \"$(dirname \"$0\")\"\n"
            f"rm -rf \"{backup}\"; mkdir -p \"{backup}\"\n"
            f"cp -r tools VERSION \"{backup}/\" 2>/dev/null || true\n"
            f"cp -r \"{staging}/.\" ./\n"
            f"echo '{new_version}' > VERSION\n"
            f"chmod +x START.sh START_CONSOLE.sh START_UI.sh 2>/dev/null || true\n"
            f"rm -rf \"{staging}\"; rm -f \"$0\"\n"
            "echo '[Update] Done. Restart ./START.sh'\n",
            encoding="utf-8")
        script.chmod(0o755)
    return script


def run_check():
    cur = current_version()
    try:
        tag, url, name = fetch_latest()
    except Exception as e:
        print(f"[Update] Cannot check: {e}")
        return 1
    if is_newer(tag, cur):
        print(f"[Update] Available: v{tag} (current v{cur}). Asset: {name}")
        print("  Run with --update to download and install.")
    else:
        print(f"[Update] You are up to date (v{cur}).")
    return 0


def run_update(yes=False):
    cur = current_version()
    try:
        tag, url, name = fetch_latest()
    except Exception as e:
        print(f"[Update] Cannot check: {e}")
        return 1
    if not is_newer(tag, cur):
        print(f"[Update] Already up to date (v{cur}).")
        return 0
    print(f"[Update] v{cur} -> v{tag} ({name})")
    if not yes and sys.stdin.isatty():
        ans = input("[Update] Download and install? [Y/n]: ").strip().lower()
        if ans in ("n", "no"):
            print("[Update] Cancelled.")
            return 0
    tmp = Path(tempfile.mkdtemp(prefix="scrcpy_update_"))
    archive = tmp / name
    staging = tmp / "staging"
    try:
        print("[Update] Downloading...")
        download(url, archive)
        print("[Update] Unpacking...")
        stage_archive(archive, staging)
        script = write_apply_script(staging, tag)
    except Exception as e:
        print(f"[Update] FAILED: {e}")
        return 1
    print(f"[Update] Staged. Applying via {script.name} after exit...")
    print("  Your old files are saved in backup_v<old>/")
    if os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "/min", str(script)],
                         cwd=str(ROOT), shell=False)
    else:
        subprocess.Popen(["bash", str(script)], cwd=str(ROOT),
                         start_new_session=True)
    return 0
