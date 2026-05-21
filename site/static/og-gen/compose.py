"""Compose final og.png from the AI-generated ghost mascot + brand chrome.

Input:  ghost-nano-banana-pro-v1.png (1024×1024, Nano Banana Pro output)
Output: ../og.png (1200×1200 final, with status pill + stat ribbon +
        wordmark + install line + hostname stamp)
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
GHOST = HERE / "ghost-v2.png"
OUT = ROOT / "og.png"

# Brand palette (matches site app.css OKLCH → hex approximations)
SURFACE = (24, 22, 20)            # #181614
SURFACE_2 = (15, 14, 12)          # #0f0e0c
INK = (247, 245, 241)             # #f7f5f1
MUTE = (155, 148, 140)            # #9b948c
MUTE_2 = (107, 102, 96)           # #6b6660
HAIR = (50, 47, 43)               # #322f2b
ACCENT = (224, 160, 80)           # #e0a050

# Font paths — DejaVu is the workhorse on Termux. Bold for wordmark,
# regular for body, Sans Mono for the technical bits.
FONT_DIR = Path("/data/data/com.termux/files/usr/share/fonts/TTF")
F_DISPLAY = str(FONT_DIR / "DejaVuSans-Bold.ttf")
F_BODY = str(FONT_DIR / "DejaVuSans.ttf")
F_MONO = str(FONT_DIR / "DejaVuSansMono-Bold.ttf")

W, H = 1200, 1200


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def main() -> None:
    canvas = Image.new("RGB", (W, H), SURFACE)
    draw = ImageDraw.Draw(canvas, "RGBA")

    # ----- subtle blueprint grid backdrop ------------------------------
    grid_color = (35, 33, 30)  # slightly above surface
    for x in range(0, W, 48):
        draw.line([(x, 0), (x, H)], fill=grid_color, width=1)
    for y in range(0, H, 48):
        draw.line([(0, y), (W, y)], fill=grid_color, width=1)
    # Fade the grid via a vertical alpha mask
    mask = Image.new("L", (W, H), 0)
    mdraw = ImageDraw.Draw(mask)
    for y in range(H):
        # 0 transparent at top + bottom, ~120 in the middle
        d_edge = min(y, H - 1 - y)
        v = max(0, min(140, int(d_edge * 0.45)))
        mdraw.line([(0, y), (W, y)], fill=v)
    overlay = Image.new("RGB", (W, H), SURFACE)
    canvas = Image.composite(canvas, overlay, mask)
    draw = ImageDraw.Draw(canvas)

    # ----- frame hairlines top + bottom --------------------------------
    draw.line([(80, 80), (W - 80, 80)], fill=HAIR, width=1)
    draw.line([(80, H - 80), (W - 80, H - 80)], fill=HAIR, width=1)

    # ----- status pill (top-left) --------------------------------------
    pill_x, pill_y = 80, 110
    pill_w, pill_h = 420, 50
    draw.rectangle([(pill_x, pill_y), (pill_x + pill_w, pill_y + pill_h)],
                   fill=SURFACE_2, outline=HAIR, width=1)
    # Pulsing dot — solid here, no animation in static image
    dot_cx, dot_cy = pill_x + 22, pill_y + pill_h // 2
    draw.ellipse([(dot_cx - 6, dot_cy - 6), (dot_cx + 6, dot_cy + 6)], fill=ACCENT)
    f_pill = font(F_MONO, 17)
    draw.text((pill_x + 42, pill_y + 14),
              "V1.2.0 · LAN-FIRST STACK",
              font=f_pill, fill=MUTE, spacing=2)

    # ----- stat ribbon (top-right) -------------------------------------
    f_stat = font(F_MONO, 15)
    # Stats line — sourced from the same site/src/lib/stats.json the
    # landing page imports. Keep this in sync with the site rebuild;
    # re-running compose.py after `python3 tools/site_stats.py` picks
    # up new figures automatically.
    import json as _json
    _stats = _json.loads((HERE / ".." / ".." / "src" / "lib" /
                           "stats.json").resolve().read_text())
    stat_text = (f"{_stats['subcommands']} SUBCOMMANDS · "
                  f"{_stats['printer_models']} MODELS · "
                  f"{_stats['tests']} TESTS")
    sw = draw.textlength(stat_text, font=f_stat)
    draw.text((W - 80 - sw, 135), stat_text, font=f_stat, fill=MUTE_2)

    # ----- ghost mascot (centered, upper half) -------------------------
    # AI source is 1024×1024 with a near-black solid background. We
    # background-blend by darkening the AI image's bg toward our SURFACE.
    ghost = Image.open(GHOST).convert("RGB")
    # Map the AI's near-pure-black to our warm graphite by adding a
    # constant offset to each channel (very subtle; only affects pixels
    # already close to black). Doing this preserves the amber pixels
    # untouched.
    from PIL import ImageChops
    bg_target = Image.new("RGB", ghost.size, SURFACE)
    # Where AI image is dark (background, panel), swap in our surface color.
    # Threshold widened from 25 to 45 to catch the subtle near-black panel
    # edge the AI bakes in. Pixels at the amber outline are >>100 R so
    # they're untouched.
    px = ghost.load()
    for y in range(ghost.height):
        for x in range(ghost.width):
            r, g, b = px[x, y]
            if r < 45 and g < 45 and b < 45:
                # Smooth blend: closer to true black gets full SURFACE,
                # near-threshold gets a partial blend to avoid hard edge
                t = max(r, g, b) / 45.0
                blend_r = int(SURFACE[0] * (1 - t) + r * t)
                blend_g = int(SURFACE[1] * (1 - t) + g * t)
                blend_b = int(SURFACE[2] * (1 - t) + b * t)
                px[x, y] = (blend_r, blend_g, blend_b)
    GW = 680
    ghost = ghost.resize((GW, GW), Image.LANCZOS)
    gx = (W - GW) // 2
    gy = 175
    canvas.paste(ghost, (gx, gy))
    draw = ImageDraw.Draw(canvas)

    # ----- wordmark (centered, below ghost) ----------------------------
    # Big enough to dominate, sized so beambam fits in 1040px content area.
    f_word = font(F_DISPLAY, 150)
    beam, bam = "beam", "bam"
    bw = draw.textlength(beam, font=f_word)
    aw = draw.textlength(bam, font=f_word)
    word_x = (W - (bw + aw)) // 2
    word_y = 870
    draw.text((word_x, word_y), beam, font=f_word, fill=ACCENT)
    draw.text((word_x + bw, word_y), bam, font=f_word, fill=INK)

    # ----- tagline -----------------------------------------------------
    f_tag = font(F_BODY, 28)
    tag = "Signed-MQTT bridge for every Bambu Lab printer."
    tw = draw.textlength(tag, font=f_tag)
    draw.text(((W - tw) // 2, 1030), tag, font=f_tag, fill=INK)

    # ----- install command (mono, centered) ----------------------------
    f_cmd = font(F_MONO, 26)
    cmd_prompt = "$ "
    cmd_text = "pip install beambam"
    cw_p = draw.textlength(cmd_prompt, font=f_cmd)
    cw_t = draw.textlength(cmd_text, font=f_cmd)
    cx = (W - (cw_p + cw_t)) // 2
    cy = 1080
    draw.text((cx, cy), cmd_prompt, font=f_cmd, fill=MUTE)
    draw.text((cx + cw_p, cy), cmd_text, font=f_cmd, fill=ACCENT)

    # ----- hostname stamp (between install line + bottom frame) -------
    f_host = font(F_MONO, 14)
    host = "BEAMBAM.BOO"
    hw = draw.textlength(host, font=f_host)
    draw.text(((W - hw) // 2, 1140), host, font=f_host, fill=MUTE_2, spacing=4)

    canvas.save(OUT, format="PNG", optimize=True)
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
