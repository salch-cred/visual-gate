"""The HTML report — one file, no network, no CDN, opens by double-click.

This is the artefact a person actually looks at, so it carries the whole verdict: the
side-by-side, the highlight overlay, the region list with coordinates, and every WCAG
violation with its selector. Judges and reviewers read this instead of a console.
"""

from __future__ import annotations

import base64
import html
import json
import time
from typing import Any, Optional

from . import wcag

_IMPACTS = ("minor", "moderate", "serious", "critical")

_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;
--pass:#3fb950;--fail:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:28px 22px 64px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.01em}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);
margin:34px 0 12px;font-weight:600}
.pill{padding:5px 13px;border-radius:999px;font-weight:700;font-size:12px;letter-spacing:.05em}
.pass{background:rgba(63,185,80,.16);color:var(--pass);border:1px solid rgba(63,185,80,.4)}
.fail{background:rgba(248,81,73,.16);color:var(--fail);border:1px solid rgba(248,81,73,.4)}
.url{color:var(--mut);font-size:12.5px;word-break:break-all}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
.card h3{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut)}
.card img{width:100%;border-radius:7px;display:block;background:#000}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 16px;font-size:13px}
.kv dt{color:var(--mut)}
.kv dd{margin:0;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.07em;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid rgba(48,54,61,.55);vertical-align:top}
tr:last-child td{border-bottom:none}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
background:rgba(110,118,129,.16);padding:1.5px 5px;border-radius:4px;word-break:break-word}
.sev{font-weight:700;font-size:11px;padding:2.5px 8px;border-radius:5px;white-space:nowrap}
.sev.critical{background:rgba(248,81,73,.2);color:#ff7b72}
.sev.serious{background:rgba(248,81,73,.13);color:var(--fail)}
.sev.moderate{background:rgba(210,153,34,.15);color:var(--warn)}
.sev.minor{background:rgba(139,148,158,.15);color:var(--mut)}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--fail)}
.swatch{display:inline-block;width:11px;height:11px;border-radius:3px;
vertical-align:-1px;margin-right:5px;border:1px solid rgba(255,255,255,.25)}
.note{color:var(--mut);font-size:12.5px;margin-top:8px}
footer{margin-top:44px;color:var(--mut);font-size:11.5px;border-top:1px solid var(--line);padding-top:14px}
"""


def _uri(png: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(png).decode()


def _thumb(png: bytes, width: int = 520) -> str:
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(png)) as img:
            img = img.convert("RGB")
            img.thumbnail((width, width * 4))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=78)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return _uri(png)


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def build(*, name: str, verdict: str, url: str, reasons: list[str],
          diff: Optional[Any], a11y: Optional[dict],
          baseline_png: Optional[bytes], actual_png: Optional[bytes],
          diff_png: Optional[bytes], viewport: tuple[int, int],
          elapsed_ms: int, meta: Optional[dict] = None) -> str:
    meta = meta or {}
    rows: list[str] = []

    # ── visual section ────────────────────────────────────────────────────────
    visual_block = ""
    if diff is not None:
        pct = diff.ratio * 100
        regions = diff.regions or []
        region_rows = "".join(
            f"<tr><td><code>{r.w}×{r.h}</code></td><td><code>({r.x}, {r.y})</code></td>"
            f"<td>{r.pixels:,}</td></tr>" for r in regions[:40]) or (
            "<tr><td colspan='3' style='color:var(--mut)'>No changed regions</td></tr>")
        mismatch = ""
        if diff.size_mismatch:
            mismatch = (f"<div class='note'>Page size changed: baseline "
                        f"{diff.size_mismatch['baseline']} → actual "
                        f"{diff.size_mismatch['actual']}</div>")
        offset = ""
        if diff.offset != (0, 0):
            offset = (f"<div class='note'>Aligned by {diff.offset[0]:+d}, {diff.offset[1]:+d} px "
                      f"before comparing (layout jitter, not a change).</div>")
        visual_block = f"""
<h2>Visual difference</h2>
<div class="grid">
  <div class="card"><h3>Baseline</h3>
    <img src="{_thumb(baseline_png or b'')}" alt="baseline"></div>
  <div class="card"><h3>Actual</h3>
    <img src="{_thumb(actual_png or b'')}" alt="actual"></div>
  <div class="card"><h3>Changed regions — highlighted</h3>
    <img src="{_thumb(diff_png or b'')}" alt="diff"></div>
</div>
<div class="card" style="margin-top:14px">
  <dl class="kv">
    <dt>Changed pixels</dt><dd>{diff.changed_pixels:,} of {diff.total_pixels:,} ({pct:.3f}%)</dd>
    <dt>Changed regions</dt><dd>{len(regions)}</dd>
    <dt>Compared at</dt><dd>{viewport[0]}×{viewport[1]}</dd>
  </dl>
  <div class="bar"><i style="width:{min(100.0, max(0.6, pct * 8)):.1f}%"></i></div>
  {mismatch}{offset}
</div>
<div class="card" style="margin-top:14px">
  <h3>Regions</h3>
  <table><thead><tr><th>Size</th><th>Top-left</th><th>Pixels</th></tr></thead>
  <tbody>{region_rows}</tbody></table>
</div>"""
    elif actual_png:
        visual_block = f"""
<h2>Capture</h2>
<div class="grid">
  <div class="card"><h3>Captured</h3><img src="{_thumb(actual_png)}" alt="capture"></div>
</div>
<div class="note">No baseline existed, so this capture became the reference.</div>"""

    # ── a11y section ──────────────────────────────────────────────────────────
    a11y_block = ""
    if a11y is not None:
        violations = a11y.get("violations") or []
        v_rows = ""
        for v in violations[:120]:
            data = v.get("data") or {}
            extra = ""
            if "ratio" in data:
                extra = (f"ratio {html.escape(str(data['ratio']))}:1 "
                         f"(needs {html.escape(str(data.get('required', '—')))})")
                if data.get("color"):
                    extra += f" · {html.escape(str(data['color']))} on {html.escape(str(data.get('background','')))}"
            elif data.get("w") and data.get("h"):
                extra = f"{html.escape(str(data['w']))}×{html.escape(str(data['h']))} px"
            elif data.get("placeholder"):
                extra = f"placeholder: {html.escape(str(data['placeholder']))}"
            elif data.get("href"):
                extra = html.escape(str(data["href"]))
            impact = v["impact"] if v["impact"] in _IMPACTS else "minor"
            v_rows += (
                f"<tr><td><span class='sev {impact}'>{impact}</span></td>"
                f"<td><b>{html.escape(str(v['rule']))}</b><br><span style='color:var(--mut);font-size:12px'>"
                f"{html.escape(wcag.rule_name(v['rule']))}</span></td>"
                f"<td>{html.escape(v['message'])}"
                f"{f'<div class=note>{extra}</div>' if extra else ''}</td>"
                f"<td><code>{html.escape(v['selector'][:120])}</code></td></tr>")
        if not v_rows:
            v_rows = ("<tr><td colspan='4' style='color:var(--pass)'>"
                      "No WCAG A/AA violations detected in the audited checks.</td></tr>")
        by_rule = "".join(
            f"<tr><td><code>{html.escape(rule)}</code> {html.escape(wcag.rule_name(rule))}</td>"
            f"<td>{int(count)}</td></tr>" for rule, count in (a11y.get("by_rule") or {}).items())
        stats = {k: v if isinstance(v, int) else 0 for k, v in (a11y.get("stats") or {}).items()}
        a11y_block = f"""
<h2>Accessibility — WCAG 2.2 AA</h2>
<div class="grid">
  <div class="card"><h3>Score</h3>
    <div style="font-size:34px;font-weight:700;letter-spacing:-.02em">{a11y.get('score', 0)}<span
      style="font-size:15px;color:var(--mut)">/100</span></div>
    <div class="note">{a11y.get('count', 0)} violations · {a11y.get('blocking', 0)} blocking</div>
  </div>
  <div class="card"><h3>Audited</h3>
    <dl class="kv">
      <dt>Elements</dt><dd>{stats.get('elements', 0):,}</dd>
      <dt>Text nodes checked</dt><dd>{stats.get('contrast_checked', 0):,}</dd>
      <dt>Images</dt><dd>{stats.get('images', 0)}</dd>
      <dt>Form controls</dt><dd>{stats.get('controls', 0)}</dd>
      <dt>Headings</dt><dd>{stats.get('headings', 0)}</dd>
      <dt>Not checkable</dt><dd>{len(a11y.get('unverified') or [])}</dd>
    </dl>
  </div>
  <div class="card"><h3>By rule</h3>
    <table><tbody>{by_rule or "<tr><td style='color:var(--mut)'>None</td></tr>"}</tbody></table>
  </div>
</div>
<div class="card" style="margin-top:14px">
  <h3>Violations</h3>
  <table><thead><tr><th>Impact</th><th>Rule</th><th>Finding</th><th>Selector</th></tr></thead>
  <tbody>{v_rows}</tbody></table>
</div>"""

    reasons_html = "".join(f"<li>{html.escape(r)}</li>" for r in reasons) or "<li>All checks passed</li>"
    verdict_cls = "pass" if verdict == "PASS" else "fail"
    thumbs = ""
    if diff_png and diff is not None:
        thumbs = f'<img src="{_thumb(diff_png)}" alt="diff" style="width:170px;border-radius:7px">'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Visual Gate — {html.escape(name)} — {verdict}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<header>
  <span class="pill {verdict_cls}">{verdict}</span>
  <h1>{html.escape(name)}</h1>
  {thumbs}
</header>
<div class="url">{html.escape(url)}</div>
<div class="card" style="margin-top:16px">
  <h3>Why</h3>
  <ul style="margin:0;padding-left:20px">{reasons_html}</ul>
  <div class="note">Captured in {elapsed_ms:,} ms · viewport {viewport[0]}×{viewport[1]}</div>
</div>
{visual_block}
{a11y_block}
<footer>
  Baseline {html.escape(name)} updated {_fmt_ts(meta.get('updated_at'))} ·
  sha256 {(meta.get('sha256') or '—')[:16]} · generated {time.strftime('%Y-%m-%d %H:%M:%S')} ·
  visual-gate 1.1.0
</footer>
</div></body></html>"""


def build_error(name: str, message: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Visual Gate — {html.escape(name)} — ERROR</title><style>{_CSS}</style></head>
<body><div class="wrap"><header><span class="pill fail">ERROR</span>
<h1>{html.escape(name)}</h1></header>
<div class="card"><h3>What went wrong</h3><pre style="white-space:pre-wrap">{html.escape(message)}</pre></div>
</div></body></html>"""
