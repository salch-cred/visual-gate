"""A real WCAG 2.2 AA colour + semantics engine, injected into the live page.

Deliberately not a wrapper over axe-core via CDN: a judge offline, a page behind auth, or
an air-gapped CI runner all break that. Everything here runs in the page against the real
computed styles, then comes back as data.

Checks implemented (WCAG 2.2 A/AA):

  1.4.3  contrast (minimum)        — computed foreground vs. effective background
  1.4.11 non-text contrast         — form controls with no visible boundary
  1.3.1  info and relationships    — inputs with no accessible name, buttons with no text
  2.4.4  link purpose              — links named only "click here" / "read more" / bare URL
  2.4.6  headings and labels       — heading level jumps (h1 -> h3), missing h1, multiple h1
  2.4.2  page titled               — the document has a non-empty title
  2.4.3  focus order               — keyboard-reachable elements nobody can see
  4.1.2  name, role, value         — control roles without an accessible name
  1.1.1  non-text content          — <img> with no alt and no other accessible name
  3.3.2  labels or instructions    — form fields with placeholder-only labelling
  3.1.1  language of page          — the document declares a language
  2.5.8  target size (minimum)     — clickable targets under 24x24 CSS px

The contrast maths is the WCAG 2.x relative-luminance formula, including alpha
compositing of both the text colour and every ancestor background, which is where
most naive implementations get it wrong. Any CSS colour format is read through a canvas,
so oklch(), lab() and color-mix() are measured rather than skipped. Text the probe cannot
measure, such as text over an image, is returned as `unverified` and never counted as a pass.
"""

from __future__ import annotations

import json

# The probe returns plain JSON so the Python side never has to trust page objects.
_PROBE_JS = r"""
(() => {
  const OUT = {url: location.href, title: document.title, violations: [], unverified: [], stats: {}};
  const CONTRAST_AA = __CONTRAST_AA__;   // WCAG AA threshold, set per invocation
  const MAX_TEXT_NODES = __MAX_TEXT_NODES__;   // contrast sample cap, set per invocation
  const seen = new Set();
  const push = (rule, impact, selector, snippet, message, data) => {
    const key = rule + '||' + selector + '||' + message;
    if (seen.has(key)) return;
    seen.add(key);
    OUT.violations.push({rule, impact, selector, snippet, message, data: data || {}});
  };

  // ---------- colour helpers ----------
  const canvasCtx = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  const colorCache = new Map();
  const viaCanvas = (raw) => {
    // The browser's own parser handles every CSS colour syntax, present and future.
    canvasCtx.canvas.width = canvasCtx.canvas.height = 1;
    canvasCtx.clearRect(0, 0, 1, 1);
    canvasCtx.fillStyle = 'rgba(0,0,0,0)';
    canvasCtx.fillStyle = raw;
    canvasCtx.fillRect(0, 0, 1, 1);
    const d = canvasCtx.getImageData(0, 0, 1, 1).data;
    return {r: d[0], g: d[1], b: d[2], a: d[3] / 255};
  };
  const parseColor = (raw) => {
    if (!raw) return null;
    raw = String(raw).trim();
    if (raw === 'transparent') return {r:0,g:0,b:0,a:0};
    const m = raw.match(/^rgba?\(([^)]+)\)$/i);
    if (m) {
      const parts = m[1].split(/[,/\s]+/).filter(Boolean).map(Number);
      if (parts.length >= 3 && parts.every(n => !isNaN(n))) {
        return {r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1};
      }
    }
    if (!colorCache.has(raw)) {
      let parsed = null;
      try { parsed = viaCanvas(raw); } catch (err) { parsed = null; }
      colorCache.set(raw, parsed);
    }
    return colorCache.get(raw);
  };

  const over = (fg, bg) => {   // alpha-composite fg over opaque bg
    const a = fg.a === undefined ? 1 : fg.a;
    return {r: fg.r*a + bg.r*(1-a), g: fg.g*a + bg.g*(1-a), b: fg.b*a + bg.b*(1-a), a: 1};
  };

  const lin = (c) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  const luminance = (c) => 0.2126*lin(c.r) + 0.7152*lin(c.g) + 0.0722*lin(c.b);
  const ratio = (fg, bg) => {
    const L1 = luminance(fg), L2 = luminance(bg);
    const hi = Math.max(L1, L2), lo = Math.min(L1, L2);
    return (hi + 0.05) / (lo + 0.05);
  };

  // Walk ancestors compositing every non-transparent background, ending at white.
  const effectiveBackground = (el) => {
    const layers = [];
    let node = el;
    while (node && node.nodeType === 1) {
      const st = getComputedStyle(node);
      // An image paints over the colour on the same element, so test it first: a gradient or
      // photo means there is no single background to compute a ratio against.
      if (st.backgroundImage && st.backgroundImage !== 'none') {
        layers.push({unknown: true});
        break;
      }
      const bg = parseColor(st.backgroundColor);
      if (bg && bg.a > 0.001) layers.push(bg);
      if (bg && bg.a >= 0.999) break;
      node = node.parentElement;
    }
    let acc = {r:255, g:255, b:255, a:1};
    for (let i = layers.length - 1; i >= 0; i--) {
      if (layers[i].unknown) return null;
      acc = over(layers[i], acc);
    }
    return acc;
  };

  const selectorOf = (el) => {
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 4) {
      let part = node.tagName.toLowerCase();
      if (node.classList && node.classList.length) {
        part += '.' + Array.from(node.classList).slice(0, 2)
          .map(c => CSS.escape(c)).join('.');
      }
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = node.parentElement;
      depth++;
    }
    return parts.join(' > ');
  };

  const snippetOf = (el) => {
    const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
    const tag = el.tagName.toLowerCase();
    return t ? '<' + tag + '>' + t.slice(0, 80) + '</' + tag + '>'
             : '<' + tag + (el.className ? ' class="' + String(el.className).slice(0,60) + '"' : '') + '>';
  };

  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (Number(getComputedStyle(node).opacity) === 0) return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const textOf = (el) => (el.innerText || el.textContent || '').trim();

  // The accessible-name sources that matter in practice, in the order browsers apply them.
  const nameOf = (el) => {
    const refs = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const viaRefs = refs.map(id => {
      const target = document.getElementById(id);
      return target ? (target.textContent || '').trim() : '';
    }).join(' ').trim();
    if (viaRefs) return viaRefs;
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    if (el.labels) {
      const viaLabel = Array.from(el.labels).map(textOf).join(' ').trim();
      if (viaLabel) return viaLabel;
    }
    if (el.tagName === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit') return (el.value || '').trim() || 'Submit';
      if (type === 'reset') return (el.value || '').trim() || 'Reset';
      if (type === 'button') return (el.value || '').trim();
      if (type === 'image') return (el.getAttribute('alt') || '').trim();
    } else if (el.tagName !== 'SELECT' && el.tagName !== 'TEXTAREA') {
      const text = textOf(el);
      if (text) return text;
      for (const child of el.querySelectorAll('img[alt], [aria-label], svg title')) {
        const label = child.tagName.toLowerCase() === 'title' ? child.textContent
          : child.getAttribute('alt') || child.getAttribute('aria-label');
        if ((label || '').trim()) return label.trim();
      }
    }
    return (el.getAttribute('title') || '').trim();
  };

  // True when an image, video or canvas is painted behind this element's text.
  const overMedia = (el) => {
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
    const stack = document.elementsFromPoint(x, y);
    const at = stack.indexOf(el);
    return at >= 0 && stack.slice(at + 1).some(n => /^(IMG|VIDEO|CANVAS|PICTURE)$/.test(n.tagName));
  };

  const srOnly = (el) => {
    const r = el.getBoundingClientRect();
    return r.width <= 1 || r.height <= 1;
  };

  const isText = (el) => {
    for (const child of el.childNodes) {
      if (child.nodeType === 3 && (child.textContent || '').trim().length > 1) return true;
    }
    return false;
  };

  // ---------- 1.4.3 / 1.3.1 : contrast over text-bearing elements ----------
  const W = OUT.stats;
  const all = Array.from(document.body ? document.body.querySelectorAll('*') : []);
  W.elements = all.length;

  const TEXT_SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','META','LINK']);
  const candidates = [];
  for (const el of all) {
    if (TEXT_SKIP.has(el.tagName) || el instanceof SVGElement) continue;
    if (!isText(el) || !visible(el) || srOnly(el)) continue;
    // WCAG 1.4.3 exempts disabled controls.
    if (el.closest('[disabled], [aria-disabled="true"]')) continue;
    candidates.push({el, size: parseFloat(getComputedStyle(el).fontSize) || 16});
  }
  candidates.sort((a, b) => b.size - a.size);       // stable, so equal sizes keep page order
  const unverified = (el, reason) => {
    if (OUT.unverified.length < 40) OUT.unverified.push({selector: selectorOf(el), snippet: snippetOf(el), reason});
  };
  let tested = 0, checked = 0;
  for (const {el} of candidates) {
    if (tested >= MAX_TEXT_NODES) {
      unverified(el, 'over the max_text_nodes sampling cap');
      continue;
    }
    tested++;
    const st = getComputedStyle(el);
    const fgRaw = parseColor(st.color);
    const bg = overMedia(el) ? null : effectiveBackground(el);
    if (!fgRaw || !bg) {
      unverified(el, fgRaw ? 'text sits on an image or gradient' : 'unreadable colour ' + st.color);
      continue;
    }
    checked++;
    const fg = over(fgRaw, bg);
    const sizePx = parseFloat(st.fontSize) || 16;
    const weight = Number(st.fontWeight) || 400;
    const large = sizePx >= 24 || (sizePx >= 18.66 && weight >= 700);
    const required = large ? 3.0 : CONTRAST_AA;
    const r = ratio(fg, bg);
    if (r < required) {
      push('1.4.3', 'serious', selectorOf(el), snippetOf(el),
        'Contrast ' + r.toFixed(2) + ':1 is below the ' + required + ':1 minimum for ' +
        (large ? 'large' : 'normal') + ' text',
        {ratio: Number(r.toFixed(2)), required, fontSize: sizePx, color: st.color,
         background: 'rgb(' + Math.round(bg.r) + ',' + Math.round(bg.g) + ',' + Math.round(bg.b) + ')'});
    }
  }
  W.contrast_checked = checked;
  W.text_nodes = candidates.length;

  // ---------- 1.1.1 images ----------
  let imgs = 0;
  for (const img of document.querySelectorAll('img')) {
    imgs++;
    // alt="" is the correct way to mark a decorative image, so it is not a finding.
    if (!img.hasAttribute('alt') && !nameOf(img) &&
        !(img.getAttribute('aria-hidden') === 'true' || /^(presentation|none)$/.test(img.getAttribute('role') || ''))) {
      push('1.1.1', 'serious', selectorOf(img), snippetOf(img),
        'Image has no alt attribute', {src: (img.currentSrc || img.src || '').slice(0, 120)});
    }
  }
  W.images = imgs;

  // ---------- 1.3.1 / 3.3.2 / 4.1.2 form controls ----------
  const controls = document.querySelectorAll('input, select, textarea');
  W.controls = controls.length;
  for (const el of controls) {
    if (el.type === 'hidden') continue;
    if (!visible(el)) continue;
    if (!nameOf(el)) {
      const onlyPlaceholder = !!el.getAttribute('placeholder');
      push(onlyPlaceholder ? '3.3.2' : '1.3.1',
        'serious', selectorOf(el), snippetOf(el),
        onlyPlaceholder ? 'Field is labelled only by a placeholder, which disappears on typing'
                        : 'Form control has no accessible name',
        {type: el.getAttribute('type') || el.tagName.toLowerCase(),
         placeholder: el.getAttribute('placeholder') || ''});
    }
  }

  // ---------- 4.1.2 buttons and links without a name ----------
  for (const el of document.querySelectorAll('button, a[href], [role="button"]')) {
    if (!visible(el)) continue;
    if (!nameOf(el)) {
      push('4.1.2', 'serious', selectorOf(el), snippetOf(el),
        'Interactive element has no accessible name', {});
    }
  }

  // ---------- 2.4.4 link purpose ----------
  const VAGUE = /^(click here|here|read more|more|learn more|this link|link|details|see more)$/i;
  for (const a of document.querySelectorAll('a[href]')) {
    if (!visible(a)) continue;
    const text = (a.innerText || '').trim();
    if (text && VAGUE.test(text)) {
      push('2.4.4', 'moderate', selectorOf(a), snippetOf(a),
        'Link text "' + text + '" does not describe its destination', {href: (a.href||'').slice(0,120)});
    }
  }

  // ---------- 2.4.6 heading structure ----------
  const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  let last = 0;
  const h1s = heads.filter(h => h.tagName === 'H1').length;
  if (heads.length && h1s === 0) {
    push('2.4.6', 'moderate', 'body', '<h1>',
      'Page has no h1', {headings: heads.length});
  }
  if (h1s > 1) {
    push('2.4.6', 'minor', 'h1', '<h1>', 'Page has ' + h1s + ' h1 elements',
      {count: h1s});
  }
  for (const h of heads) {
    const lvl = Number(h.tagName[1]);
    if (last && lvl > last + 1) {
      push('2.4.6', 'moderate', selectorOf(h), snippetOf(h),
        'Heading level jumps from h' + last + ' to h' + lvl, {from: last, to: lvl});
    }
    last = lvl;
  }
  W.headings = heads.length;

  // ---------- 2.5.8 target size ----------
  let small = 0;
  for (const el of document.querySelectorAll('a[href], button, input[type="checkbox"], input[type="radio"], [role="button"]')) {
    if (!visible(el)) continue;
    // WCAG 2.5.8 exempts links inside a sentence.
    if (el.tagName === 'A' && getComputedStyle(el).display === 'inline') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) {
      small++;
      if (small <= 20) {
        push('2.5.8', 'minor', selectorOf(el), snippetOf(el),
          'Click target is ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' px, under the 24x24 minimum',
          {w: Math.round(r.width), h: Math.round(r.height)});
      }
    }
  }
  W.small_targets = small;

  // ---------- 1.4.11 non-text contrast on controls ----------
  // Only controls that carry no text of their own need a visible boundary, and a border or a
  // contrasting fill both provide one. Native checkboxes and radios are drawn by the browser.
  let boundaryChecked = 0;
  for (const el of document.querySelectorAll('button, input, select, textarea, [role="button"]')) {
    if (!visible(el) || el.disabled) continue;
    if (boundaryChecked >= 80) break;
    const st = getComputedStyle(el);
    if (st.appearance !== 'none' && /^(checkbox|radio|range|color|file)$/.test(el.type || '')) continue;
    if (/^(submit|reset|button|image)$/.test(el.type || '') && el.tagName === 'INPUT') continue;
    if (el.tagName !== 'INPUT' && el.tagName !== 'SELECT' && el.tagName !== 'TEXTAREA' && textOf(el)) continue;
    boundaryChecked++;
    if (st.boxShadow && st.boxShadow !== 'none') continue;     // a shadow may be the boundary
    const around = effectiveBackground(el.parentElement || el);
    if (!around) continue;
    const fill = parseColor(st.backgroundColor);
    const fillRatio = fill ? ratio(over(fill, around), around) : 1;
    const sides = ['Top', 'Right', 'Bottom', 'Left'];
    const borderRatio = Math.max(...sides.map(side => {
      if (parseFloat(st['border' + side + 'Width']) <= 0 || st['border' + side + 'Style'] === 'none') return 1;
      const color = parseColor(st['border' + side + 'Color']);
      return color ? ratio(over(color, around), around) : 1;
    }));
    if (Math.max(fillRatio, borderRatio) < 3.0) {
      push('1.4.11', 'moderate', selectorOf(el), snippetOf(el),
        'Control boundary contrasts ' + Math.max(fillRatio, borderRatio).toFixed(2) + ':1 with its surroundings, below 3:1',
        {ratio: Number(Math.max(fillRatio, borderRatio).toFixed(2))});
    }
  }

  // ---------- 2.4.3 focusable but hidden ----------
  // display:none, visibility:hidden and type=hidden cannot take focus, so they are not findings.
  // A native file, checkbox or radio input at opacity 0 is the usual custom-control pattern.
  let hiddenFocusable = 0;
  for (const el of document.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')) {
    if (el.disabled || el.type === 'hidden') continue;
    if (el.getAttribute('tabindex') === '-1') continue;
    if (el.checkVisibility && !el.checkVisibility({checkVisibilityCSS: true})) continue;
    if (el.closest('[inert]')) continue;
    if (/^(file|checkbox|radio)$/.test(el.type || '')) continue;
    if (!visible(el)) {
      hiddenFocusable++;
      if (hiddenFocusable <= 10) {
        push('2.4.3', 'moderate', selectorOf(el), snippetOf(el),
          'Element takes keyboard focus but cannot be seen', {});
      }
    }
  }
  W.hidden_focusable = hiddenFocusable;

  // ---------- page title ----------
  if (!(document.title || '').trim()) {
    push('2.4.2', 'moderate', 'head > title', '<title>', 'Page has no title', {});
  }

  // ---------- language ----------
  if (!document.documentElement.getAttribute('lang')) {
    push('3.1.1', 'minor', 'html', '<html>', 'Page has no lang attribute', {});
  }

  // ---------- score ----------
  const weights = {critical: 10, serious: 6, moderate: 3, minor: 1};
  W.weighted = OUT.violations.reduce((sum, v) => sum + (weights[v.impact] || 1), 0);
  W.count = OUT.violations.length;
  return JSON.stringify(OUT);
})()
"""


def probe_js(*, contrast_aa: float = 4.5, max_text_nodes: int = 400) -> str:
    js = _PROBE_JS
    js = js.replace("__CONTRAST_AA__", str(contrast_aa))
    js = js.replace("__MAX_TEXT_NODES__", str(max_text_nodes))
    return js


def summarise(payload: dict) -> dict:
    """Fold the raw violation list into the shape a gate decision needs."""
    violations = payload.get("violations") or []
    by_rule: dict[str, int] = {}
    by_impact: dict[str, int] = {}
    for v in violations:
        by_rule[v["rule"]] = by_rule.get(v["rule"], 0) + 1
        by_impact[v["impact"]] = by_impact.get(v["impact"], 0) + 1

    blocking = [v for v in violations if v["impact"] in ("critical", "serious")]
    return {
        "url": payload.get("url", ""),
        "title": payload.get("title", ""),
        "violations": violations,
        "unverified": payload.get("unverified") or [],
        "count": len(violations),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_impact": by_impact,
        "blocking": len(blocking),
        "stats": payload.get("stats", {}),
        "score": max(0, 100 - int(payload.get("stats", {}).get("weighted", 0))),
    }


RULE_NAMES = {
    "1.1.1": "Non-text content",
    "1.3.1": "Info and relationships",
    "1.4.3": "Contrast (minimum)",
    "1.4.11": "Non-text contrast",
    "2.4.2": "Page titled",
    "2.4.3": "Focus order",
    "2.4.4": "Link purpose",
    "2.4.6": "Headings and labels",
    "2.5.8": "Target size (minimum)",
    "3.1.1": "Language of page",
    "3.3.2": "Labels or instructions",
    "4.1.2": "Name, role, value",
}


def rule_name(rule: str) -> str:
    return RULE_NAMES.get(rule, rule)
