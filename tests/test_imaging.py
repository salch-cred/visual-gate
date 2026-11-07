"""The diff engine, no browser needed. The first test is the bug that matters most."""

import io

import numpy as np
from PIL import Image, ImageDraw
from visual_gate import imaging


def png(draw_fn, size=(1280, 800), bg="white"):
    img = Image.new("RGB", size, bg)
    draw_fn(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def layout(dx=0, dy=0, fg="black"):
    def draw(d):
        d.rectangle((40 + dx, 30 + dy, 340 + dx, 90 + dy), fill="#0645ad")
        for row in range(12):
            d.text((40 + dx, 120 + dy + row * 18), "the quick brown fox " * 4, fill=fg)
    return draw


def test_identical_images_never_differ():
    for size in ((1280, 800), (375, 700), (1280, 3200)):
        shot = png(layout(), size)
        result = imaging.diff(shot, shot)
        assert result.changed_pixels == 0
        assert result.offset == (0, 0)


def test_one_pixel_jitter_is_aligned_away():
    result = imaging.diff(png(layout()), png(layout(dx=1, dy=1)))
    assert result.changed_pixels == 0
    assert result.offset != (0, 0)


def test_three_pixel_shift_is_a_real_change():
    result = imaging.diff(png(layout()), png(layout(dx=3, dy=3)))
    assert result.offset == (0, 0)
    assert result.ratio > 0.001


def test_alignment_does_not_invent_a_change_on_dark_pages():
    base = png(layout(fg="white"), bg="#101418")
    moved = png(layout(dx=1, fg="white"), bg="#101418")
    assert imaging.diff(base, moved).changed_pixels == 0


def test_real_edit_is_reported_with_its_region():
    def edited(d):
        layout()(d)
        d.rectangle((600, 400, 640, 440), fill="black")

    result = imaging.diff(png(layout()), png(edited))
    assert result.changed_pixels == 41 * 41
    box = result.regions[0]
    assert box.x <= 600 and box.x + box.w >= 640 and box.y <= 400 and box.y + box.h >= 440


def test_size_change_is_flagged():
    result = imaging.diff(png(layout(), (800, 600)), png(layout(), (800, 640)))
    assert result.size_mismatch == {"baseline": [800, 600], "actual": [800, 640]}


def test_highlight_image_matches_the_compared_pixels():
    result = imaging.diff(png(layout()), png(layout(dx=1)))
    with Image.open(io.BytesIO(result.diff_png)) as img:
        pixels = np.asarray(img.convert("RGB"))
    assert not (pixels == np.array([255, 0, 90])).all(axis=2).any()
