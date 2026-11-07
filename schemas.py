"""Tool schemas. Written so the model knows exactly *when* to gate rather than guesser."""

_VP = {
    "type": "object",
    "description": "Viewport used for capture. Default 1280x800 at 1x.",
    "properties": {
        "width": {"type": "integer", "default": 1280},
        "height": {"type": "integer", "default": 800},
        "device_scale": {"type": "number", "default": 1.0},
    },
}

VG_CAPTURE = {
    "name": "vg_capture",
    "description": (
        "Screenshot a web page in a real headless Chrome and either store it as a new visual "
        "baseline or compare it with the stored one. Use `set_baseline=true` the first time (or "
        "after an intentional design change) and omit it afterwards. With a baseline present it "
        "returns PASS/FAIL, the changed-pixel ratio and the bounding box of every changed region. "
        "Reach for this after editing CSS, markup, layout or theme code — a unit test cannot see a "
        "button that moved off-screen or text that became unreadable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "Stable baseline name, e.g. 'home-desktop' or 'checkout-mobile'."},
            "url": {"type": "string",
                    "description": "http(s) URL, or a local file path / file:// URL."},
            "viewport": _VP,
            "full_page": {"type": "boolean", "default": False,
                          "description": "Capture the whole scroll height, not just the viewport."},
            "set_baseline": {"type": "boolean", "default": False,
                             "description": "Overwrite the baseline with this capture."},
            "wait_ms": {"type": "integer", "default": 350,
                        "description": "Extra settle time after load — raise it for animated pages."},
            "selector": {"type": "string",
                         "description": "Capture only this element (CSS selector) instead of the page."},
            "report": {"type": "boolean", "default": True,
                       "description": "Write a self-contained HTML report and return its path."},
        },
        "required": ["name", "url"],
    },
}

VG_DIFF = {
    "name": "vg_diff",
    "description": (
        "Compare two images already on disk (PNG/JPG) and report the changed-pixel ratio plus every "
        "changed region. Use for screenshots taken outside Hermes, or to compare two captures "
        "directly. Pixel alignment and an antialiasing threshold are applied so sub-pixel font "
        "rendering does not read as a change."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "baseline": {"type": "string", "description": "Path to the reference image."},
            "actual": {"type": "string", "description": "Path to the image to check."},
            "threshold": {"type": "integer",
                          "description": "Per-channel delta (0-255) counted as a change. Default from settings."},
            "tolerance": {"type": "number",
                          "description": "Fraction of changed pixels still considered a pass."},
            "name": {"type": "string", "description": "Label for the report."},
            "report": {"type": "boolean", "default": True},
        },
        "required": ["baseline", "actual"],
    },
}

VG_A11Y = {
    "name": "vg_a11y",
    "description": (
        "Audit a live page for WCAG 2.2 AA violations: computed colour-contrast ratios (with alpha "
        "compositing of every ancestor background), images without alt text, form controls with no "
        "accessible name, placeholder-only labels, heading level jumps, vague link text, undersized "
        "click targets, focusable-but-hidden elements and missing page language. Returns every "
        "finding pinned to a CSS selector plus a 0-100 score. Use it on any UI you build or modify — "
        "it catches problems invisible in a screenshot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http(s) URL, file path or file:// URL."},
            "viewport": _VP,
            "wait_ms": {"type": "integer", "default": 350},
            "min_impact": {"type": "string", "enum": ["minor", "moderate", "serious", "critical"],
                           "description": "Only report findings at or above this impact."},
            "limit": {"type": "integer", "default": 60,
                      "description": "Max findings returned (report always has all of them)."},
            "report": {"type": "boolean", "default": True},
        },
        "required": ["url"],
    },
}

VG_GATE = {
    "name": "vg_gate",
    "description": (
        "The one call that answers 'did my change break the UI'. Captures the page, diffs it against "
        "the baseline and audits accessibility in the same browser session, then returns a single "
        "PASS/FAIL with the reasons, the changed regions and the blocking accessibility findings. "
        "This is the tool to run after touching front-end code, before telling anyone it works. "
        "Add `set_baseline=true` to bless the current state instead of failing against it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Baseline name."},
            "url": {"type": "string", "description": "http(s) URL or local file path."},
            "viewport": _VP,
            "full_page": {"type": "boolean", "default": False},
            "set_baseline": {"type": "boolean", "default": False},
            "skip_visual": {"type": "boolean", "default": False,
                            "description": "Run only the accessibility half."},
            "skip_a11y": {"type": "boolean", "default": False,
                          "description": "Run only the visual half."},
            "tolerance": {"type": "number",
                          "description": "Fraction of changed pixels still passing. Default from settings."},
            "threshold": {"type": "integer", "description": "Per-channel change threshold."},
            "max_blocking": {"type": "integer", "default": 0,
                             "description": "How many serious+critical a11y findings are tolerated."},
            "wait_ms": {"type": "integer", "default": 350},
            "report": {"type": "boolean", "default": True},
        },
        "required": ["name", "url"],
    },
}

VG_BASELINES = {
    "name": "vg_baselines",
    "description": (
        "List, inspect or delete stored visual baselines. `action='list'` shows each baseline's URL, "
        "viewport and age; `action='delete'` removes one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "delete"], "default": "list"},
            "name": {"type": "string", "description": "Required for delete."},
        },
        "required": [],
    },
}

VG_DOCTOR = {
    "name": "vg_doctor",
    "description": (
        "Check that visual-gate can actually run here: finds a Chrome/Chromium binary, launches it, "
        "confirms CDP, screenshots a data-URL page and reports timing. Call this first when a "
        "capture fails, or right after install."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

ALL = [VG_CAPTURE, VG_DIFF, VG_A11Y, VG_GATE, VG_BASELINES, VG_DOCTOR]
