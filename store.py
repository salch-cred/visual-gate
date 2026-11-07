"""Baselines on disk, under the plugin's own data directory.

Layout:

  <hermes home>/plugin-data/visual-gate/
    baselines/<name>/baseline.png     the reference image
    baselines/<name>/meta.json        viewport, device scale, url, sha, timestamps
    baselines/<name>/shots/<ts>.png   recent captures, pruned to keep_shots
    reports/<name>-<ts>.html          self-contained report, open-in-browser

No database: a baseline is a file plus a small json, which means a user can inspect,
copy, git-track or delete one with the tools they already have.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional

PLUGIN = "visual-gate"
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class StoreError(ValueError):
    pass


def root() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN)
    except Exception:
        from hermes_constants import get_hermes_home
        p = get_hermes_home() / "plugin-data" / PLUGIN
        p.mkdir(parents=True, exist_ok=True)
        return p


def safe_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_OK.fullmatch(name) or ".." in name:
        raise StoreError(f"invalid baseline name {name!r}: letters, digits, dot, dash, underscore only")
    return name


def baseline_dir(name: str) -> Path:
    p = root() / "baselines" / safe_name(name)
    p.mkdir(parents=True, exist_ok=True)
    (p / "shots").mkdir(exist_ok=True)
    return p


def read_baseline(name: str) -> Optional[tuple[bytes, dict]]:
    d = baseline_dir(name)
    png = d / "baseline.png"
    if not png.is_file():
        return None
    meta: dict = {}
    meta_file = d / "meta.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    return png.read_bytes(), meta


def write_baseline(name: str, png: bytes, meta: dict) -> Path:
    d = baseline_dir(name)
    (d / "baseline.png").write_bytes(png)
    payload = dict(meta)
    payload.update({
        "name": safe_name(name),
        "sha256": hashlib.sha256(png).hexdigest(),
        "bytes": len(png),
        "created_at": payload.get("created_at") or time.time(),
        "updated_at": time.time(),
    })
    (d / "meta.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return d / "baseline.png"


def keep_shot(name: str, png: bytes, limit: int) -> Path:
    d = baseline_dir(name) / "shots"
    path = d / f"{int(time.time() * 1000)}.png"
    path.write_bytes(png)
    shots = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in shots[max(1, int(limit)):]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def write_report(name: str, html: str) -> Path:
    d = root() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{safe_name(name)}-{int(time.time())}.html"
    path.write_text(html, encoding="utf-8")
    # keep the newest 40 reports
    reports = sorted(d.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in reports[40:]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def list_baselines() -> list[dict]:
    base = root() / "baselines"
    if not base.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        png = d / "baseline.png"
        if not png.is_file():
            continue
        meta: dict = {}
        mf = d / "meta.json"
        if mf.is_file():
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        out.append({
            "name": d.name,
            "url": meta.get("url", ""),
            "viewport": meta.get("viewport"),
            "sha256": meta.get("sha256", "")[:12],
            "updated_at": meta.get("updated_at"),
            "bytes": meta.get("bytes", png.stat().st_size),
            "shots": len(list((d / "shots").glob("*.png"))) if (d / "shots").is_dir() else 0,
        })
    return out


def delete_baseline(name: str) -> bool:
    d = root() / "baselines" / safe_name(name)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False
