"""Tool handlers.

Handler contract: ``def handler(args, **kwargs) -> str`` returning a JSON string, never
raising. Settings come from a module-level _SETTINGS dict that register() fills, because
Hermes does not hand ``ctx`` to the handler.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import cdp, imaging, render, store, wcag

_CTX: Any = None            # set by configure(ctx); the real PluginContext (no ctx is passed
_SETTINGS: dict = {}       # to handlers, so config is read off the ctx at call time


def configure(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    _SETTINGS.clear()


def _cfg(key: str, default: Any = None) -> Any:
    if _CTX is not None:
        try:
            return _CTX.get_config(key, default)
        except Exception:
            pass
    return _SETTINGS.get(key, default)


def _num(args: dict, key: str, default: Any, cast: Any = float) -> Any:
    """An explicit 0 is a real value, so this tests for None rather than truthiness."""
    value = args.get(key)
    if value is None:
        value = _cfg(key, default) if key in ("threshold", "tolerance") else default
    return cast(value)


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False, default=str)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False, default=str)


# ── input normalisation ─────────────────────────────────────────────────────────

def normalise_url(raw: str) -> str:
    """Accept a URL, a file path, a bare file:// string or a data: URL."""
    url = (raw or "").strip()
    if not url:
        raise ValueError("url is required")
    if url.startswith(("http://", "https://", "file://", "data:", "about:")):
        return url
    path = Path(url).expanduser()
    if path.exists():
        return path.resolve().as_uri()
    if url.startswith("localhost") or url[0].isdigit() and ":" in url:
        return "http://" + url
    return url


def check_url(url: str) -> None:
    """Apply the operator's Hermes URL rules, the same floor the built-in browser tools use.

    Loopback and private hosts stay allowed because a local dev server is the main target. Only
    http(s) URLs are judged; outside Hermes these helpers do not exist and nothing is enforced.
    """
    if not url.startswith(("http://", "https://")):
        return
    try:
        from tools.url_safety import is_always_blocked_url
        from tools.website_policy import check_website_access
    except ImportError:
        return
    if is_always_blocked_url(url):
        raise PermissionError("Blocked: URL targets a cloud metadata endpoint")
    blocked = check_website_access(url)
    if blocked:
        raise PermissionError(blocked.get("message") or "Blocked by the website policy")


def _open(browser: cdp.Browser, url: str, args: dict) -> None:
    """Navigate, then re-check the final address so a redirect cannot step around the policy."""
    check_url(url)
    wait = _num(args, "wait_ms", 350, int)
    browser.navigate(url, wait=max(0.0, wait / 1000.0),
                     allow_error_status=bool(args.get("allow_error_status")))
    check_url(str(browser.evaluate("location.href") or ""))


def _viewport(args: dict) -> tuple[int, int, float]:
    vp = args.get("viewport") or {}
    width = int(vp.get("width") or 1280)
    height = int(vp.get("height") or 800)
    scale = float(vp.get("device_scale") or 1.0)
    width = max(240, min(width, 3840))
    height = max(240, min(height, 4000))
    scale = max(0.5, min(scale, 3.0))
    return width, height, scale


def _chrome() -> str:
    chrome = cdp.find_chrome(_cfg("chrome_path", ""))
    if not chrome:
        raise RuntimeError(
            "No Chrome/Chromium/Edge binary found. Install one, or set the plugin setting "
            "chrome_path (or the VISUAL_GATE_CHROME env var) to its full path."
        )
    return chrome


class Session:
    """One browser, reused for capture + audit so the gate pays launch cost once."""

    def __init__(self, width: int, height: int, scale: float) -> None:
        self.width, self.height, self.scale = width, height, scale
        self._browser: Optional[cdp.Browser] = None

    def __enter__(self) -> "cdp.Browser":
        self._browser = cdp.Browser(_chrome(), width=self.width, height=self.height,
                                    device_scale=self.scale)
        return self._browser.__enter__()

    def __exit__(self, *exc: Any) -> None:
        if self._browser is not None:
            self._browser.__exit__(*exc)


def _capture(browser: cdp.Browser, url: str, args: dict, *, full_page: bool,
             selector: str = "") -> tuple[bytes, int, int]:
    _open(browser, url, args)
    if selector:
        clip = browser.evaluate(
            "(() => { const el = document.querySelector(%s); if (!el) return null;"
            " const r = el.getBoundingClientRect();"
            " return {x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: r.height}; })()"
            % json.dumps(selector))
        if not clip:
            raise ValueError(f"selector not found on the page: {selector!r}")
        result = browser._call("Page.captureScreenshot", {
            "format": "png",
            "clip": {"x": clip["x"], "y": clip["y"],
                     "width": max(1, clip["w"]), "height": max(1, clip["h"]), "scale": 1},
            "captureBeyondViewport": True,
        }, timeout=90)
        import base64
        data = base64.b64decode(result.get("data") or "")
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as img:
            return data, img.width, img.height
    shot = browser.screenshot(full_page=full_page)
    return shot.png, shot.width, shot.height


_IMPACT_ORDER = ["minor", "moderate", "serious", "critical"]


def _audit(browser: cdp.Browser, min_impact: str = "minor") -> dict:
    raw = browser.evaluate(wcag.probe_js(contrast_aa=float(_cfg("contrast_aa", 4.5)),
                                          max_text_nodes=int(_cfg("max_text_nodes", 400))),
                          timeout=120)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (ValueError, TypeError):
        raise RuntimeError("the accessibility probe returned unreadable data")
    summary = wcag.summarise(payload)
    floor = _IMPACT_ORDER.index(min_impact) if min_impact in _IMPACT_ORDER else 0
    kept = [v for v in summary["violations"] if _IMPACT_ORDER.index(v["impact"]) >= floor]
    kept.sort(key=lambda v: -_IMPACT_ORDER.index(v["impact"]))
    summary["violations"] = kept
    return summary


# ── vg_capture ─────────────────────────────────────────────────────────────────

def vg_capture(args: dict, **kwargs) -> str:
    name = str(args.get("name") or "").strip()
    try:
        store.safe_name(name)
        url = normalise_url(str(args.get("url") or ""))
        width, height, scale = _viewport(args)
        set_baseline = bool(args.get("set_baseline"))
        full_page = bool(args.get("full_page"))
        selector = str(args.get("selector") or "")
        started = time.time()

        with Session(width, height, scale) as browser:
            png, px_w, px_h = _capture(browser, url, args, full_page=full_page, selector=selector)

        prior = store.read_baseline(name)
        meta = {"url": url, "viewport": [width, height], "device_scale": scale,
                "full_page": full_page, "selector": selector, "pixels": [px_w, px_h]}

        if prior is None or set_baseline:
            store.write_baseline(name, png, meta)
            store.keep_shot(name, png, int(_cfg("keep_shots", 40)))
            elapsed = int((time.time() - started) * 1000)
            report_path = ""
            if args.get("report", True):
                html = render.build(name=name, verdict="PASS", url=url,
                                    reasons=["Baseline written — nothing to compare against yet"],
                                    diff=None, a11y=None, baseline_png=None, actual_png=png,
                                    diff_png=None, viewport=(px_w, px_h), elapsed_ms=elapsed,
                                    meta=meta)
                report_path = str(store.write_report(name, html))
            return _ok(mode="baseline-written", name=name, pixels=[px_w, px_h],
                       elapsed_ms=elapsed, report=report_path or None,
                       **({"replaced": True} if prior else {}))

        baseline_png, prior_meta = prior
        result = imaging.diff(baseline_png, png, threshold=_num(args, "threshold", 12, int),
                              align=bool(_cfg("align", True)))
        tolerance = _num(args, "tolerance", 0.001)
        passed = result.ratio <= tolerance and not result.size_mismatch
        store.keep_shot(name, png, int(_cfg("keep_shots", 40)))
        elapsed = int((time.time() - started) * 1000)

        reasons = []
        if result.size_mismatch:
            reasons.append(f"Page size changed from {result.size_mismatch['baseline']} to "
                           f"{result.size_mismatch['actual']}")
        if result.ratio > tolerance:
            reasons.append(f"{result.changed_pixels:,} px changed ({result.ratio * 100:.3f}%), "
                           f"over the {tolerance * 100:.3f}% tolerance")
        if not reasons:
            reasons.append("No visual change beyond tolerance")

        report_path = ""
        if args.get("report", True):
            html = render.build(name=name, verdict="PASS" if passed else "FAIL", url=url,
                                reasons=reasons, diff=result, a11y=None,
                                baseline_png=baseline_png, actual_png=png,
                                diff_png=result.diff_png, viewport=(px_w, px_h),
                                elapsed_ms=elapsed, meta=prior_meta)
            report_path = str(store.write_report(name, html))

        return _ok(
            mode="compare", name=name, passed=passed,
            changed_pixels=result.changed_pixels, ratio=round(result.ratio, 6),
            tolerance=tolerance, regions=[r.as_dict() for r in result.regions[:20]],
            size_mismatch=result.size_mismatch, aligned_by=list(result.offset),
            pixels=[px_w, px_h], elapsed_ms=elapsed, report=report_path or None,
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", tool="vg_capture")


# ── vg_diff ────────────────────────────────────────────────────────────────────

def vg_diff(args: dict, **kwargs) -> str:
    try:
        base_path = Path(str(args.get("baseline") or "")).expanduser()
        act_path = Path(str(args.get("actual") or "")).expanduser()
        for p, label in ((base_path, "baseline"), (act_path, "actual")):
            if not p.is_file():
                return _err(f"{label} image not found: {p}")
        started = time.time()
        result = imaging.diff(
            base_path.read_bytes(), act_path.read_bytes(),
            threshold=_num(args, "threshold", 12, int),
            align=bool(_cfg("align", True)))
        tolerance = _num(args, "tolerance", 0.001)
        passed = result.ratio <= tolerance and not result.size_mismatch
        elapsed = int((time.time() - started) * 1000)
        name = str(args.get("name") or base_path.stem)
        reasons = []
        if result.size_mismatch:
            reasons.append(f"Image size changed from {result.size_mismatch['baseline']} to "
                           f"{result.size_mismatch['actual']}")
        reasons.append(f"{result.changed_pixels:,} px differ ({result.ratio * 100:.3f}%); "
                       f"tolerance {tolerance * 100:.3f}%")
        report_path = ""
        if args.get("report", True):
            html = render.build(name=name, verdict="PASS" if passed else "FAIL",
                                url=f"{base_path} vs {act_path}", reasons=reasons,
                                diff=result, a11y=None,
                                baseline_png=base_path.read_bytes(),
                                actual_png=act_path.read_bytes(),
                                diff_png=result.diff_png,
                                viewport=result.actual_size, elapsed_ms=elapsed)
            report_path = str(store.write_report(name, html))
        return _ok(passed=passed, changed_pixels=result.changed_pixels,
                   ratio=round(result.ratio, 6), tolerance=tolerance,
                   regions=[r.as_dict() for r in result.regions[:20]],
                   size_mismatch=result.size_mismatch, elapsed_ms=elapsed,
                   report=report_path or None)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", tool="vg_diff")


# ── vg_a11y ────────────────────────────────────────────────────────────────────

def vg_a11y(args: dict, **kwargs) -> str:
    try:
        url = normalise_url(str(args.get("url") or ""))
        width, height, scale = _viewport(args)
        started = time.time()
        with Session(width, height, scale) as browser:
            _open(browser, url, args)
            summary = _audit(browser, str(args.get("min_impact") or "minor"))
            shot = browser.screenshot()
        elapsed = int((time.time() - started) * 1000)
        limit = max(1, min(int(args.get("limit") or 60), 400))
        findings = summary["violations"][:limit]
        report_path = ""
        if args.get("report", True):
            html = render.build(
                name=str(args.get("name") or "a11y"), verdict="PASS" if summary["blocking"] == 0 else "FAIL",
                url=url, reasons=[f"{summary['count']} violations, {summary['blocking']} serious or critical"]
                + _unverified_note(summary),
                diff=None, a11y=summary, baseline_png=None, actual_png=shot.png,
                diff_png=None, viewport=(shot.width, shot.height), elapsed_ms=elapsed)
            report_path = str(store.write_report("a11y", html))
        return _ok(score=summary["score"], count=summary["count"],
                   blocking=summary["blocking"], by_rule=summary["by_rule"],
                   by_impact=summary["by_impact"], stats=summary["stats"],
                   unverified=summary["unverified"], violations=findings, truncated=len(summary["violations"]) > limit,
                   elapsed_ms=elapsed, report=report_path or None)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", tool="vg_a11y")


# ── vg_gate ────────────────────────────────────────────────────────────────────

def _unverified_note(summary: dict) -> list[str]:
    """A check that could not run is not a pass, so the verdict has to say so."""
    count = len(summary.get("unverified") or [])
    if not count:
        return []
    return [f"Accessibility: {count} text element(s) could not be contrast-checked "
            f"(image behind the text, or over the sampling cap); review those by eye"]


def vg_gate(args: dict, **kwargs) -> str:
    name = str(args.get("name") or "").strip()
    try:
        store.safe_name(name)
        url = normalise_url(str(args.get("url") or ""))
        width, height, scale = _viewport(args)
        set_baseline = bool(args.get("set_baseline"))
        skip_visual = bool(args.get("skip_visual"))
        skip_a11y = bool(args.get("skip_a11y"))
        tolerance = _num(args, "tolerance", 0.001)
        threshold = _num(args, "threshold", 12, int)
        max_blocking = int(args.get("max_blocking") if args.get("max_blocking") is not None else 0)

        started = time.time()
        with Session(width, height, scale) as browser:
            png, px_w, px_h = _capture(browser, url, args, full_page=bool(args.get("full_page")))
            a11y_summary = None
            if not skip_a11y:
                a11y_summary = _audit(browser, "minor")

        reasons: list[str] = []
        diff_result = None
        prior = store.read_baseline(name)
        visual_passed = True
        meta = {"url": url, "viewport": [width, height], "device_scale": scale,
                "full_page": bool(args.get("full_page")), "pixels": [px_w, px_h]}

        if skip_visual:
            pass
        elif prior is None or set_baseline:
            store.write_baseline(name, png, meta)
            reasons.append("Baseline " + ("updated" if prior else "created") +
                           " from this run (nothing to compare against)")
        else:
            baseline_png, prior_meta = prior
            meta = prior_meta or meta
            diff_result = imaging.diff(baseline_png, png, threshold=threshold,
                                       align=bool(_cfg("align", True)))
            visual_passed = diff_result.ratio <= tolerance and not diff_result.size_mismatch
            if diff_result.size_mismatch:
                reasons.append(f"Page size changed from {diff_result.size_mismatch['baseline']} "
                               f"to {diff_result.size_mismatch['actual']}")
            if diff_result.ratio > tolerance:
                reasons.append(f"{diff_result.changed_pixels:,} px changed "
                               f"({diff_result.ratio * 100:.3f}% > {tolerance * 100:.3f}% tolerance)")
            if visual_passed:
                reasons.append("Visual: no change beyond tolerance")
            if diff_result.offset != (0, 0):
                reasons.append(f"Visual: compared after aligning by {diff_result.offset[0]:+d}, "
                               f"{diff_result.offset[1]:+d} px; the whole page moved that far")

        a11y_passed = True
        if a11y_summary is not None:
            blocking = [v for v in a11y_summary["violations"]
                        if v["impact"] in ("serious", "critical")]
            a11y_passed = len(blocking) <= max_blocking
            if blocking:
                tally: dict[str, int] = {}
                for v in blocking:
                    tally[v["rule"]] = tally.get(v["rule"], 0) + 1
                top = ", ".join(f"{r}×{c}" for r, c in list(tally.items())[:4])
                reasons.append(f"Accessibility: {len(blocking)} serious/critical "
                               f"(allowed {max_blocking}) — {top}")
            else:
                reasons.append(f"Accessibility: no serious or critical findings "
                               f"(score {a11y_summary['score']}/100)")
            reasons.extend(_unverified_note(a11y_summary))

        passed = visual_passed and a11y_passed
        store.keep_shot(name, png, int(_cfg("keep_shots", 40)))
        elapsed = int((time.time() - started) * 1000)

        report_path = ""
        if args.get("report", True):
            html = render.build(
                name=name, verdict="PASS" if passed else "FAIL", url=url, reasons=reasons,
                diff=diff_result, a11y=a11y_summary,
                baseline_png=prior[0] if prior and not set_baseline else None,
                actual_png=png, diff_png=diff_result.diff_png if diff_result else None,
                viewport=(px_w, px_h), elapsed_ms=elapsed, meta=meta)
            report_path = str(store.write_report(name, html))

        payload = {
            "verdict": "PASS" if passed else "FAIL",
            "passed": passed,
            "name": name,
            "url": url,
            "reasons": reasons,
            "visual": None if skip_visual else {
                "passed": visual_passed,
                "changed_pixels": diff_result.changed_pixels if diff_result else 0,
                "ratio": round(diff_result.ratio, 6) if diff_result else 0.0,
                "tolerance": tolerance,
                "regions": [r.as_dict() for r in (diff_result.regions[:15] if diff_result else [])],
                "compared": diff_result is not None,
                "aligned_by": list(diff_result.offset) if diff_result else [0, 0],
                "baseline_written": prior is None or set_baseline,
            },
            "a11y": None if a11y_summary is None else {
                "passed": a11y_passed,
                "score": a11y_summary["score"],
                "count": a11y_summary["count"],
                "blocking": a11y_summary["blocking"],
                "by_rule": a11y_summary["by_rule"],
                "unverified": len(a11y_summary["unverified"]),
                "top": a11y_summary["violations"][:12],
            },
            "pixels": [px_w, px_h],
            "elapsed_ms": elapsed,
            "report": report_path or None,
        }
        return json.dumps({"ok": True, **payload}, ensure_ascii=False, default=str)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", tool="vg_gate", name=name)


# ── vg_baselines / vg_doctor ───────────────────────────────────────────────────

def vg_baselines(args: dict, **kwargs) -> str:
    try:
        action = str(args.get("action") or "list")
        if action == "delete":
            name = str(args.get("name") or "")
            if not name:
                return _err("name is required for delete")
            removed = store.delete_baseline(name)
            return _ok(action="delete", name=name, removed=removed,
                       **({} if removed else {"error": "no such baseline"}))
        items = store.list_baselines()
        return _ok(action="list", count=len(items), baselines=items,
                   **({} if items else {"hint": "No baselines yet. Call vg_gate with "
                                                "set_baseline=true to create one."}))
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", tool="vg_baselines")


def vg_doctor(args: dict, **kwargs) -> str:
    report: dict[str, Any] = {"ok": True}
    chrome = cdp.find_chrome(_cfg("chrome_path", ""))
    report["chrome"] = chrome or None
    if not chrome:
        report["ok"] = False
        report["error"] = ("No Chrome/Chromium/Edge found. Install one or set the chrome_path "
                           "plugin setting.")
        report["searched"] = cdp._CANDIDATES[:6] + ["PATH: chrome, chromium, google-chrome, msedge"]
        return json.dumps(report, ensure_ascii=False, default=str)

    page = ("data:text/html," + quote(
        "<!doctype html><html lang='en'><head><title>gate</title></head>"
        "<body style='font:16px system-ui;background:#fff;color:#111'>"
        "<h1 style='margin:24px'>visual-gate self test</h1>"
        "<p style='margin:24px;color:#767676'>Low contrast paragraph for the audit probe.</p>"
        "<img src='x.png'><button style='margin:24px;width:18px;height:18px'></button>"
        "</body></html>"))

    try:
        started = time.time()
        with Session(1280, 800, 1.0) as browser:
            shot = browser.screenshot()
            launch_ms = int((time.time() - started) * 1000)
            browser.navigate(page, wait=0.1)
            raw = browser.evaluate(wcag.probe_js(), timeout=60)
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            summary = wcag.summarise(payload)
            audit_shot = browser.screenshot()
        report.update({
            "ok": True,
            "screenshot": {"width": shot.width, "height": shot.height,
                           "bytes": len(shot.png)},
            "launch_and_shot_ms": launch_ms,
            "audit": {
                "score": summary["score"],
                "violations": summary["count"],
                "by_rule": summary["by_rule"],
                "self_test_expected": ">=2 rules fired (contrast, alt text, target size)",
                "passed": summary["count"] >= 2,
            },
            "imaging": {"pillow": _pillow_version(), "numpy": _numpy_version()},
            "data_dir": str(store.root()),
        })
        if summary["count"] < 2:
            report["ok"] = False
            report["error"] = ("the audit probe ran but found nothing on a page built to fail — "
                               "the CDP evaluate path is not returning the probe result")
    except Exception as exc:
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    return json.dumps(report, ensure_ascii=False, default=str)


def _pillow_version() -> str:
    try:
        import PIL
        return getattr(PIL, "__version__", "?")
    except Exception:
        return "missing"


def _numpy_version() -> str:
    try:
        import numpy
        return getattr(numpy, "__version__", "?")
    except Exception:
        return "missing"
