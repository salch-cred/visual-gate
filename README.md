# visual-gate

**Your agent says the UI is fine. This checks.**

An agent edits CSS, the tests stay green, and a button has quietly moved off-screen. visual-gate gives Hermes a real browser to verify its own front-end work: it screenshots the page, compares it with a stored baseline, and audits WCAG 2.2 AA on the same render. One tool call returns PASS or FAIL with the reasons.

A prompt or a skill cannot do this. They can only ask the model to look at code. visual-gate measures the rendered page: real pixels, real computed styles.

## Tools

| Tool | What it does |
|---|---|
| `vg_gate` | One call: capture, diff, accessibility audit, HTML report. Returns PASS/FAIL with reasons. |
| `vg_capture` | Store a baseline, or compare against one. Supports capturing a single element by CSS selector. |
| `vg_diff` | Compare two images in the working directory or the plugin's own captures. |
| `vg_a11y` | Accessibility audit only, with a 0-100 score. |
| `vg_baselines` | List or delete stored baselines. |
| `vg_doctor` | Self-check: finds a browser, launches it, runs the audit probe. Run this first. |

These are agent tools, not CLI commands. Ask Hermes in plain language, for example "gate the checkout page at localhost:3000". Hermes loads them on demand through `tool_search`.

## Install

```bash
hermes plugins install https://github.com/salch-cred/visual-gate
hermes plugins enable visual-gate
```

You need Chrome, Edge or Chromium installed. There is no Node or Playwright dependency. Python dependencies are `Pillow` and `numpy`, both with upper bounds.

## What the gate guarantees

**A page that did not load is an error, never a verdict.** Refused connections, DNS failures and HTTP 4xx/5xx responses return an error and write no baseline. Otherwise Chrome's own error page would be judged, and could be saved as your reference. Set `allow_error_status=true` if you really do want to audit an error page.

**Unchanged means unchanged.** A shift of up to 2 px is aligned only if it demonstrably explains the difference, by at least halving the error. Any larger move is reported as a real layout change. Animations are frozen before capture: finite ones are captured at their end state, infinite ones on their first frame.

**A check that could not run is not a pass.** Text over a photo or gradient has no single background colour, and text beyond the sampling cap is not examined. Those elements are listed as `unverified` and named in the reasons, so a person can look at them.

**Text below AA blocks the gate.** Any text under the required ratio is `serious`, including 4.3:1 against a 4.5:1 minimum. Ratios use real alpha compositing over ancestor backgrounds, and every CSS colour format is read through the browser's own parser, including `oklch()`, `lab()` and `color-mix()`.

## Accessibility checks

12 WCAG 2.2 A/AA criteria, measured in the live page: 1.1.1 alt text, 1.3.1 and 3.3.2 form labels, 1.4.3 text contrast, 1.4.11 control boundaries, 2.4.2 page title, 2.4.3 hidden focus targets, 2.4.4 link purpose, 2.4.6 heading structure, 2.5.8 target size, 3.1.1 page language, 4.1.2 accessible names.

This is an automated audit, so it finds what can be measured. It does not replace testing with a screen reader, and passing it does not mean a page is fully WCAG conformant.

## The report

`vg_gate` writes one self-contained HTML file with no network dependency: verdict and reasons, baseline, actual and highlighted-diff images, changed regions with coordinates, the accessibility score, and every finding with its CSS selector.

## Settings

Set under `plugins.entries.visual-gate.settings` in `config.yaml`.

| Setting | Default | Meaning |
|---|---|---|
| `chrome_path` | `""` (auto-detect) | Path to Chrome, Edge or Chromium. `VISUAL_GATE_CHROME` also works. |
| `threshold` | `12` | Per-channel delta (0-255) counted as a change. |
| `tolerance` | `0.001` | Fraction of changed pixels still passing, so a small edit on a very tall page can pass. `0` means exact. |
| `align` | `true` | Align a shift of up to 2 px when it explains the difference. |
| `contrast_aa` | `4.5` | Contrast ratio required for normal-size text. |
| `max_text_nodes` | `400` | Text elements contrast-checked, largest text first. The rest are `unverified`. |
| `keep_shots` | `40` | Screenshots kept per baseline. |

## What it touches

- **Launches a local headless browser** with a temporary profile, which is removed afterwards. The browser gets a minimal environment (path, home, locale, temp and display variables, plus the system folders Windows needs), so provider API keys and other Hermes secrets never reach it. Under root, as in most containers, it passes `--no-sandbox` because Chrome will not start otherwise. Don't point a root-run gate at untrusted pages.
- **Loads the URL you give it**, which runs that page's JavaScript. Loopback and private addresses are allowed because a local dev server is the main target. Cloud metadata endpoints and the sites in your Hermes website blocklist are refused, including after redirects.
- **Reads local pages only under strict rules.** A local `.html`, `.htm` or `.svg` page is accepted only if its real path is inside the session working directory and Hermes' own read guard allows it, the same check `read_file` uses, so `.env` files and Hermes credential stores are refused. Every `file://` load the page makes, iframes and images included, is held and refused unless it passes the same check. `vg_diff` accepts image files only, from the working directory or the plugin's own captures. Finding snippets returned to the agent can include text from the page.
- **Writes only** under `<hermes home>/plugin-data/visual-gate/`: baselines, screenshots and reports.
- **Sends nothing anywhere.** There is no telemetry, and no third-party service is called.

## How it captures

Chrome runs headless and is driven over the DevTools Protocol by a small client built on the standard library (`cdp.py`). For the audit, a probe runs inside the page and returns computed styles as data (`wcag.py`).

## Development

```bash
pytest tests
```

The suite covers the diff engine without a browser, then runs end to end through real headless Chrome, skipping those tests if no browser is installed. It includes a case for each failure the gate must never repeat, and pages with valid markup that must stay clean.

## License

MIT
