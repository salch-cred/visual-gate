# visual-gate

**The UI gate for agent-made web work.** Screenshots a live page through a real Chrome, diffs it against a stored baseline, and audits it for WCAG 2.2 AA in the same render pass.

```text
hermes vg_gate name="checkout" url=https://store.internal/checkout
→ PASS, changed 12px (0.001%), no serious accessibility findings
```

It exists because nothing else sees what the agent broke: a unit test can't tell that a button moved off-screen, a diff library can't compute whether two fonts actually look the same, and a screenshot is just a picture. Here, because it is a real browser under your control.

---

## What it does

| Tool | What it does |
|---|---|
| `vg_gate` | One call: capture → diff → a11y → report. Returns PASS/FAIL with reasons and writes a self-contained HTML report. |
| `vg_capture` | Shoot a baseline. Use `set_baseline=true` once (or after a redesign); omit it afterwards and it diffs live. |
| `vg_diff` | Pixel-diff any two images on disk. |
| `vg_a11y` | Runs ~14 real WCAG checks against computed styles: contrast, alt text, name/role, heading order, placeholder-only labels, focusable-but-hidden, link purpose, target size. |
| `vg_baselines` | List / delete stored visual baselines. |
| `vg_doctor` | Self-check: finds Chrome, launches, screenshots, runs the probe. Call this first when a capture fails. |

**The differentiator:** the accessibility engine is a real probe that runs inside the page and returns *computed* contrast ratios — full alpha compositing over every ancestor background, not a colour pallet guess. The a11y tooling exists in 1,063 catalog entries and essentially none of them compute it against the real DOM.

---

## The report

`vg_gate` writes a single file with no network dependency. Every finding is pinned to its CSS selector and the whole thing opens instantly:

1. PASS/FAIL verdict and the one-line reasons
2. Baseline vs. actual vs. highlighted-diff thumbnails
3. Changed-region boxes with coordinates and pixel counts
4. Accessibility score, per-rule tallies and every violation with data (ratio, target size, placeholder value, etc.)

## How the diff is not a liar

- **Alignment search** — the page shifts a pixel when a font finishes loading; we search a small integer window and align first, otherwise a font switch reads as a 40% regression.
- **Per-channel threshold** — antialiasing deltas under the `threshold` setting are ignored.
- **Cellular region grouping** — changed pixels are merged into boxes (`340×60 at (12, 8)`) instead of an uncommentable pixel count.

## Install & run

```bash
hermes plugins install https://github.com/salch-cred/visual-gate
hermes plugins enable visual-gate
```

`vg_doctor` tells you whether it can find Chrome and whether the probe works. Once a baseline exists, run:

- `vg_gate name=home url=https://example.com --report` — the full verdict
- `vg_gate name=home url=https://example.com --set-baseline=true` — bless the current state
- `vg_gate name=home url=https://example.com --skip-a11y` — visual only
- `vg_gate name=home url=https://example.com --skip-visual` — accessibility only

## How it captures

Chrome headless (or `-new`) is launched with `--remote-debugging-port=0`, CDP over its own WebSocket (hand-rolled; no Playwright/Node), Navigate → `/Page.captureScreenshot` (or `Page.getLayoutMetrics` for full page) → for a11y, a probe is `Runtime.evaluate`'d in the page and returns **real computed styles**, not a screenshot guess. If no browser is running, everything degrades gracefully to a usable error.

Dependencies are `Pillow` and `numpy` only, both pinned with upper bounds.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `chrome_path` | `""` (auto-detect) | Path to Chrome/Edge/Chromium. Also `VISUAL_GATE_CHROME` env var. |
| `threshold` | `12` | Per-channel delta counted as a change. Raise if font antialiasing false-positives. |
| `tolerance` | `0.001` | Fraction of changed pixels still passing. |
| `align` | `true` | Search small offsets before diffing (see above). |
| `keep_shots` | `40` | Retain screenshots per baseline. |

## Development

```bash
pytest              # smoke tests: capture, diff, a11y probe, render, gate
```

The probe and diff are easily unit-tested without a browser (rendered HTML fixtures go in `test/render`). The browser layer is isolated behind `cdp.py` so it can be swapped for Playwright later.
