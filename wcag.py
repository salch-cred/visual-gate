"""A real WCAG 2.2 AA colour + semantics engine, injected into the live page.

Deliberately not a wrapper over axe-core via CDN: a judge offline, a page behind auth, or
an air-gapped CI runner all break that. Everything here runs in the page against the real
computed styles, then comes back as data.

Checks implemented (WCAG 2.2 A/AA):

  1.4.3  contrast (minimum)        — computed foreground vs. effective background
  1.4.11 non-text contrast         — interactive controls vs. their background
  1.3.1  info and relationships    — inputs with no accessible name, buttons with no text
  2.4.4  link purpose              — links named only "click here" / "read more" / bare URL
  2.4.6  headings and labels       — heading level jumps (h1 -> h3), missing h1, multiple h1
  2.4.3  focus order               — focusable elements hidden behind overflow/visibility
  4.1.2  name, role, value         — control roles without an accessible name
  1.1.1  non-text content          — <img> without alt, decorative images with alt=""
  3.3.2  labels or instructions    — form fields with placeholder-only labelling
  2.5.8  target size (minimum)     — clickable targets under 24x24 CSS px

The contrast maths is the WCAG 2.x relative-luminance formula, including alpha
compositing of both the text colour and every ancestor background, which is where
most naive implementations get it wrong.
"""

from __future__ import annotations

import json

# The probe returns plain JSON so the Python side never has to trust page objects.
_PROBE_JS = r"""
(() => {
  const OUT = {url: location.href, title: document.title, violations: [], stats: {}};
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
  const parseColor = (raw) => {
    if (!raw) return null;
    raw = String(raw).trim();
    if (raw === 'transparent') return {r:0,g:0,b:0,a:0};
    let m = raw.match(/^rgba?\(([^)]+)\)$/i);
    if (m) {
      const parts = m[1].split(/[,/\s]+/).filter(Boolean).map(Number);
      return {r:parts[0]||0, g:parts[1]||0, b:parts[2]||0,
              a: parts.length > 3 ? (isNaN(parts[3]) ? 1 : parts[3]) : 1};
    }
    m = raw.match(/^color\(srgb\s+([^)]+)\)$/i);
    if (m) {
      const p = m[1].split(/[\s/]+/).filter(Boolean).map(Number);
      return {r:(p[0]||0)*255, g:(p[1]||0)*255, b:(p[2]||0)*255, a: p.length>3?p[3]:1};
    }
    if (raw.startsWith('#')) {
      let h = raw.slice(1);
      if (h.length === 3) h = h.split('').map(c => c + c).join('');
      if (h.length === 8) {
        return {r:parseInt(h.slice(0,2),16), g:parseInt(h.slice(2,4),16),
                b:parseInt(h.slice(4,6),16), a:parseInt(h.slice(6,8),16)/255};
      }
      if (h.length === 6) {
        return {r:parseInt(h.slice(0,2),16), g:parseInt(h.slice(2,4),16),
                b:parseInt(h.slice(4,6),16), a:1};
      }
    }
    return null;
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
      const bg = parseColor(st.backgroundColor);
      if (bg && bg.a > 0.001) layers.push(bg);
      if (bg && bg.a >= 0.999) break;
      if (st.backgroundImage && st.backgroundImage !== 'none') {
        // A gradient/photo means we cannot compute a reliable ratio.
        layers.push({unknown: true});
        break;
      }
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
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
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

  const TEXT_SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','PATH','HEAD','META','LINK']);
  let tested = 0;
  for (const el of all) {
    if (TEXT_SKIP.has(el.tagName)) continue;
    if (!isText(el)) continue;
    if (!visible(el)) continue;
    if (tested >= MAX_TEXT_NODES) break;
    tested++;

    const st = getComputedStyle(el);
    const fgRaw = parseColor(st.color);
    const bg = effectiveBackground(el);
    if (!fgRaw || !bg) continue;
    const fg = over(fgRaw, bg);
    const sizePx = parseFloat(st.fontSize) || 16;
    const weight = Number(st.fontWeight) || 400;
    const large = sizePx >= 24 || (sizePx >= 18.66 && weight >= 700);
    const required = large ? 3.0 : CONTRAST_AA;
    const r = ratio(fg, bg);
    W.contrast_checked = tested;
    if (r < required) {
      push('1.4.3', r < required - 1.5 ? 'serious' : 'moderate', selectorOf(el), snippetOf(el),
        'Contrast ' + r.toFixed(2) + ':1 is below the ' + required + ':1 minimum for ' +
        (large ? 'large' : 'normal') + ' text',
        {ratio: Number(r.toFixed(2)), required, fontSize: sizePx, color: st.color,
         background: 'rgb(' + Math.round(bg.r) + ',' + Math.round(bg.g) + ',' + Math.round(bg.b) + ')'});
    }
  }
  W.text_nodes = tested;

  // ---------- 1.1.1 images ----------
  let imgs = 0;
  for (const img of document.querySelectorAll('img')) {
    imgs++;
    if (!img.hasAttribute('alt')) {
      push('1.1.1', 'serious', selectorOf(img), snippetOf(img),
        'Image has no alt attribute', {src: (img.currentSrc || img.src || '').slice(0, 120)});
    } else if (img.getAttribute('alt') === '' &&
               !(img.getAttribute('aria-hidden') === 'true' || img.getAttribute('role') === 'presentation')) {
      push('1.1.1', 'minor', selectorOf(img), snippetOf(img),
        'Image has empty alt but is not marked decorative', {});
    }
  }
  W.images = imgs;

  // ---------- 1.3.1 / 3.3.2 / 4.1.2 form controls ----------
  const controls = document.querySelectorAll('input, select, textarea');
  W.controls = controls.length;
  for (const el of controls) {
    if (el.type === 'hidden') continue;
    if (!visible(el)) continue;
    const id = el.getAttribute('id');
    const labelled = (id && document.querySelector('label[for="' + CSS.escape(id) + '"]')) ||
                     el.closest('label') ||
                     el.getAttribute('aria-label') ||
                     el.getAttribute('aria-labelledby') ||
                     el.getAttribute('title');
    if (!labelled) {
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
    const name = (el.innerText || el.textContent || '').trim() ||
                 el.getAttribute('aria-label') ||
                 el.getAttribute('title') ||
                 (el.querySelector('img[alt]') ? el.querySelector('img[alt]').alt : '');
    if (!name) {
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
  let borderChecked = 0;
  for (const el of document.querySelectorAll('button, input, select, textarea, [role="button"]')) {
    if (!visible(el)) continue;
    if (borderChecked >= 80) break;
    borderChecked++;
    const st = getComputedStyle(el);
    const bgEl = parseColor(st.backgroundColor);
    const parentBg = effectiveBackground(el.parentElement || el);
    if (!bgEl || !parentBg || bgEl.a < 0.05) continue;
    const composited = over(bgEl, parentBg);
    const r = ratio(composited, parentBg);
    if (r < 3.0) {
      push('1.4.11', 'moderate', selectorOf(el), snippetOf(el),
        'Control background contrasts ' + r.toFixed(2) + ':1 with its surroundings, below 3:1',
        {ratio: Number(r.toFixed(2))});
    }
  }

  // ---------- 2.4.3 focusable but hidden ----------
  let hiddenFocusable = 0;
  for (const el of document.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')) {
    if (el.disabled) continue;
    if (el.getAttribute('tabindex') === '-1') continue;
    if (!visible(el)) {
      hiddenFocusable++;
      if (hiddenFocusable <= 10) {
        push('2.4.3', 'moderate', selectorOf(el), snippetOf(el),
          'Element is focusable but not visible', {});
      }
    }
  }
  W.hidden_focusable = hiddenFocusable;

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
