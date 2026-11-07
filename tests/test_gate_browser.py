"""End to end through a real headless browser. Skipped when no Chrome/Edge is installed."""

import json
import socket

import pytest
from visual_gate import cdp, render, tools

pytestmark = pytest.mark.skipif(not cdp.find_chrome(), reason="needs Chrome, Edge or Chromium")

GOOD = ("<h1>Checkout</h1><p>Your order is ready to pay for today.</p>"
        "<div style='width:300px;height:60px;background:#0645ad'></div>")


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def gate(**args):
    return json.loads(tools.vg_gate(args))


def a11y_gate(url):
    return gate(name="page", url=url, skip_visual=True)


# ---- the verdict must be true when nothing changed ------------------------------------------

def test_unchanged_page_passes(site):
    url = site.add("good", GOOD)
    assert gate(name="same", url=url)["visual"]["compared"] is False
    second = gate(name="same", url=url)
    assert second["verdict"] == "PASS"
    assert second["visual"]["changed_pixels"] == 0


def test_unchanged_tall_page_passes(site):
    url = site.add("tall", "<h1>T</h1>" + "".join(f"<p>Row {i}</p>" for i in range(400)))
    gate(name="tall", url=url, full_page=True, skip_a11y=True)
    assert gate(name="tall", url=url, full_page=True, skip_a11y=True)["verdict"] == "PASS"


def test_animated_page_is_stable(site):
    url = site.add("spin", "<h1>S</h1><div class=s></div>",
                   css=".s{width:80px;height:80px;background:#0645ad;animation:r 1s linear infinite}"
                       "@keyframes r{to{transform:rotate(360deg)}}")
    gate(name="spin", url=url, skip_a11y=True)
    assert gate(name="spin", url=url, skip_a11y=True)["verdict"] == "PASS"


def test_fade_in_is_captured_finished(site):
    url = site.add("fade", "<h1 class=f>Hello</h1>",
                   css=".f{animation:in 9s forwards}@keyframes in{from{opacity:0}to{opacity:1}}")
    shot = json.loads(tools.vg_a11y({"url": url}))
    assert shot["stats"]["contrast_checked"] == 1


# ---- a real change must fail ----------------------------------------------------------------

def test_visual_regressions_fail(site):
    url = site.add("base", GOOD)
    gate(name="reg", url=url)
    moved = site.add("moved", GOOD, css="body{padding-top:3px;padding-left:3px}")
    assert gate(name="reg", url=moved)["verdict"] == "FAIL"


def test_zero_tolerance_is_honoured(site):
    url = site.add("t0", GOOD)
    gate(name="tol", url=url, skip_a11y=True)
    speck = site.add("t1", GOOD + "<i style='position:absolute;left:600px;top:400px;width:9px;"
                                  "height:9px;background:#000'></i>")
    assert gate(name="tol", url=speck, skip_a11y=True)["verdict"] == "PASS"
    strict = gate(name="tol", url=speck, skip_a11y=True, tolerance=0)
    assert strict["verdict"] == "FAIL"
    assert strict["visual"]["tolerance"] == 0


# ---- a page that did not load must never be judged ------------------------------------------

@pytest.mark.parametrize("target", ["http://127.0.0.1:{port}/", "http://this-host-does-not-exist.invalid/"])
def test_unreachable_page_is_an_error_and_writes_no_baseline(target):
    result = gate(name="dead", url=target.format(port=closed_port()), set_baseline=True, skip_a11y=True)
    assert result["ok"] is False
    assert "could not load" in result["error"]
    assert json.loads(tools.vg_baselines({}))["count"] == 0


@pytest.mark.parametrize("status", [404, 500])
def test_http_error_is_an_error(site, status):
    url = site.add(f"e{status}", "<h1>Oops</h1><p>Something went wrong here.</p>", status=status)
    result = gate(name="err", url=url)
    assert result["ok"] is False
    assert f"HTTP {status}" in result["error"]
    assert json.loads(tools.vg_baselines({}))["count"] == 0
    allowed = gate(name="err", url=url, allow_error_status=True)
    assert allowed["verdict"] == "PASS"


# ---- text that fails WCAG AA must fail the gate ---------------------------------------------

@pytest.mark.parametrize("color", [
    "#7a7a7a",                                # 4.3:1, only just under AA
    "#8f8f8f",                                # 3.2:1
    "oklch(0.9 0.02 250)",                    # Tailwind v4 style
    "color-mix(in oklab, white 90%, black)",
    "lab(90 0 0)",
])
def test_low_contrast_fails(site, color):
    url = site.add(f"c{abs(hash(color))}", f"<h1>Hi</h1><p style='color:{color}'>Body text here.</p>")
    result = a11y_gate(url)
    assert result["verdict"] == "FAIL"
    assert "1.4.3" in result["a11y"]["by_rule"]


def test_failing_text_past_the_cap_is_still_found_when_large(site):
    url = site.add("cap", "<h1>Hi</h1>" + "<p>ok text here</p>" * 450 +
                   "<h2 style='color:#ddd;font-size:40px'>Huge unreadable headline</h2>")
    assert a11y_gate(url)["verdict"] == "FAIL"


def test_text_over_the_cap_is_reported_unverified(site):
    url = site.add("over", "<h1>Hi</h1>" + "<p>ok text here</p>" * 450)
    result = a11y_gate(url)
    assert result["a11y"]["unverified"] > 0
    assert any("could not be contrast-checked" in r for r in result["reasons"])


def test_background_image_is_not_measured_as_its_fallback_colour(site):
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACwAAAAAAQABAAACAkQBADs="
    url = site.add("img", f"<h1>Hi</h1><div style=\"background:#000 url('{pixel}');color:#ccc\">Text</div>")
    result = a11y_gate(url)
    assert result["a11y"]["unverified"] == 1


def test_text_over_a_sibling_image_is_unverified(site):
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACwAAAAAAQABAAACAkQBADs="
    url = site.add("hero", f"<h1>Hi</h1><div style='position:relative;width:300px;height:100px'>"
                           f"<img alt='' src='{pixel}' style='position:absolute;inset:0;width:100%;height:100%'>"
                           f"<p style='position:relative;color:#eee;margin:0'>Over the photo</p></div>")
    assert a11y_gate(url)["a11y"]["unverified"] == 1


# ---- valid pages must stay clean ------------------------------------------------------------

@pytest.mark.parametrize("key,body", [
    ("submit", "<h1>F</h1><form><label for=a>Email</label> <input id=a>"
               "<input type=submit value='Send message' style='height:32px'></form>"),
    ("labelledby", "<h1>T</h1><span id=l>Close dialog</span> "
                   "<button aria-labelledby=l style='width:32px;height:32px;background:#0645ad'></button>"),
    ("svgname", "<h1>T</h1><button style='width:32px;height:32px;background:#0645ad'>"
                "<svg role=img aria-label=Close width=16 height=16></svg></button>"),
    ("hidden", "<h1>T</h1><form><input type=hidden name=csrf value=x></form>"
               "<nav style='display:none'><a href=/a>A</a></nav>"),
    ("bordered", "<h1>T</h1><label for=a>Name</label> "
                 "<input id=a style='background:#fff;border:2px solid #333;height:30px'>"),
    ("decorative", "<h1>T</h1><img alt='' width=20 height=20 src='data:image/gif;base64,"
                   "R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=='>"),
    ("disabled", "<h1>T</h1><button disabled style='height:32px;color:#bbb'>Not yet</button>"),
    ("inlinelink", "<h1>T</h1><p>Read the <a href=/docs>documentation pages</a> first.</p>"),
])
def test_valid_markup_has_no_findings(site, key, body):
    result = a11y_gate(site.add(key, body))
    assert result["verdict"] == "PASS"
    assert result["a11y"]["count"] == 0, result["a11y"]["top"]


# ---- real accessibility failures are still caught -------------------------------------------

@pytest.mark.parametrize("key,body,rule", [
    ("noalt", "<h1>T</h1><img width=20 height=20 src='data:,'>", "1.1.1"),
    ("noname", "<h1>T</h1><button style='width:32px;height:32px;background:#0645ad'></button>", "4.1.2"),
    ("placeholder", "<h1>T</h1><input placeholder=Email style='height:30px'>", "3.3.2"),
    ("nolabel", "<h1>T</h1><input style='height:30px'>", "1.3.1"),
])
def test_real_failures_block(site, key, body, rule):
    result = a11y_gate(site.add(key, body))
    assert result["verdict"] == "FAIL"
    assert rule in result["a11y"]["by_rule"]


def test_page_with_no_title_is_reported(site):
    site.add("untitled", "<h1>T</h1>")
    url = site.add("untitled", "<h1>T</h1>", title="")
    assert "2.4.2" in a11y_gate(url)["a11y"]["by_rule"]


def test_doctor_passes():
    assert json.loads(tools.vg_doctor({}))["ok"] is True


# ---- report safety --------------------------------------------------------------------------

def test_report_escapes_values_a_hostile_page_controls():
    payload = "<script>alert(1)</script>"
    html = render.build(
        name="n", verdict="FAIL", url="u", reasons=[], diff=None, baseline_png=None, actual_png=None,
        diff_png=None, viewport=(1, 1), elapsed_ms=1,
        a11y={"violations": [{"rule": payload, "impact": payload, "selector": payload,
                              "message": payload, "data": {"ratio": payload, "required": payload}}],
              "by_rule": {payload: 1}, "stats": {"elements": payload}, "unverified": []})
    assert "<script>" not in html
