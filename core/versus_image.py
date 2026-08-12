"""Pure versus-image composition for Discord (PIL, no network, no app state).

The public share pages moved to the standalone wannspieltbig-social-preview
service (2026-08); this module keeps the composition core the bot needs for
30-min reminders, ping cards and event covers. It is mirrored in that
service's image.py — changes here MUST be mirrored there manually.
"""

from __future__ import annotations

import io
import logging
import math

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("roaringbot.versus")

W, H = 1600, 800  # reference canvas (2:1)
BIG_LOGO_PATH = "resources/big_square.png"

GAME_BG = {"cs": "resources/cs-bg.jpg", "lol": "resources/lol-bg.jpg", "tm": "resources/tm-bg.jpg"}
GAME_LOGO = {"cs": "resources/cs-logo.png", "lol": "resources/lol-logo.png", "tm": "resources/tm-logo.png"}


def compose_versus_image(opponent_png: bytes, *, game: str, tournament: str,
                         bo_text: str, time_str: str,
                         w: int = W, h: int = H,
                         show_tournament: bool = True,
                         show_game_logo: bool = True,
                         show_info: bool = True) -> bytes:
    """Composite versus thumbnail: game-specific background, BIG's square
    logo, opponent logo, game logo bottom-left, match info on dark
    triangle overlays in the corners. Output is JPEG quality 85.

    Dimensions default to 2:1 (1600×800); pass h=400 for 4:1 event covers.
    The three show_* flags strip individual design elements:
      show_tournament  — the tournament label
      show_game_logo   — the game logo in the bottom band
      show_info        — the BO/date/time text
    Defaults reproduce the original full composition byte-for-byte."""
    # Scale factor relative to the reference 800 px canvas
    sf = h / 800.0
    logo_max = int(495 * sf * 1.13)
    gap = int(200 * sf)
    text_h = int(220 * sf)
    mid_y = h // 2
    band_top = h - text_h
    dy = int(w * math.tan(math.radians(10)))

    # Background
    canvas = _load_background(game)
    if (w, h) != (W, H):
        canvas = canvas.resize((w, h), Image.LANCZOS)

    # Team logos
    big = _crop_visible(_load_club_logo())
    try:
        opponent = Image.open(io.BytesIO(opponent_png)).convert("RGBA")
        if not isinstance(opponent_png, bytes) or len(opponent_png) < 100:
            raise ValueError("invalid opponent image")
        opponent = _crop_visible(opponent)
    except Exception:
        opponent = _make_tba_placeholder()

    def scale_logo(img):
        r = logo_max / max(img.width, img.height)
        return img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), Image.LANCZOS)

    big_f = scale_logo(big)
    opp_f = scale_logo(opponent)

    draw = ImageDraw.Draw(canvas)

    # Dark triangle overlays (CS only)
    if game == "cs":
        draw.polygon([(0, h), (0, h - dy), (w, h)], fill=(0, 0, 0, 175))
        draw.polygon([(w, 0), (0, 0), (w, dy)], fill=(0, 0, 0, 175))

    # Logo placement
    combined_w = big_f.width + gap + opp_f.width
    margin = (w - combined_w) // 2
    big_pos = (margin, mid_y - big_f.height // 2)
    opp_pos = (margin + big_f.width + gap, mid_y - opp_f.height // 2)

    for pos, logo in ((big_pos, big_f), (opp_pos, opp_f)):
        shadow = _make_shadow(logo)
        sp = (pos[0] + (logo.width - shadow.width) // 2,
              pos[1] + (logo.height - shadow.height) // 2)
        canvas.paste(shadow, sp, shadow)

    canvas.paste(big_f, big_pos, big_f)
    canvas.paste(opp_f, opp_pos, opp_f)

    # Fonts
    font_size = int(60 * sf)
    font_tourn_size = int(38 * sf)
    try:
        font_time = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        font_time_bold = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        font_tournament = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_tourn_size)
    except OSError:
        font_time = font_time_bold = font_tournament = ImageFont.load_default()

    # Text measurement — when the info text is hidden, the game logo
    # scales against the raw band height instead.
    if show_info:
        bo_prefix = f"{bo_text} · " if bo_text else ""
        bbox_bo = draw.textbbox((0, 0), bo_prefix, font=font_time)
        bbox_ts = draw.textbbox((0, 0), time_str, font=font_time_bold)
        th = max(bbox_bo[3] - bbox_bo[1], bbox_ts[3] - bbox_ts[1])
        tw_bo = bbox_bo[2] - bbox_bo[0]
        tw_ts = bbox_ts[2] - bbox_ts[0]
    else:
        bo_prefix = tw_bo = tw_ts = None
        bbox_ts = None
        th = text_h

    # Game logo + BO/time bottom-left
    game_logo = _load_game_logo(game) if show_game_logo else None
    optical_offset = int(42 * sf)
    if game_logo:
        gl = game_logo
        if game in ("cs", "lol"):
            t, b, l, r = _visible_bounds(gl)
            gl = gl.crop((l, t, r + 1, b + 1))
            gl_scale_val = th / gl.height
            gl_w = int(gl.width * gl_scale_val)
            gl_h = int(gl.height * gl_scale_val)
            gl = gl.resize((gl_w, gl_h), Image.LANCZOS)
            gl_x = int(36 * sf)
            centre_y = band_top + text_h // 2 + optical_offset
            ty = centre_y - th // 2 - (bbox_ts[1] if bbox_ts else 0)
            gl_y = ty
        else:
            scale_mult = 0.80
            gl_scale_val = text_h / gl.height * scale_mult
            gl_w, gl_h = int(gl.width * gl_scale_val), int(gl.height * gl_scale_val)
            gl = gl.resize((gl_w, gl_h), Image.LANCZOS)
            gl_x = int(36 * sf)
            centre_y = band_top + text_h // 2 + optical_offset
            ty = centre_y - th // 2 - (bbox_ts[1] if bbox_ts else 0)
            gl_y = band_top + (text_h - gl_h) // 2 + optical_offset
        canvas.paste(gl, (gl_x, gl_y), gl)

        if show_info:
            tx = gl_x + gl_w + int(24 * sf)
            if bo_text:
                draw.text((tx, ty), bo_prefix, fill=(255, 255, 255, 200), font=font_time)
                draw.text((tx + tw_bo, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)
            else:
                draw.text((tx, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)
    elif show_info:
        total_w = tw_bo + tw_ts
        tx = (w // 2 - total_w) // 2 - bbox_bo[0]
        ty = h - dy // 2 - th // 2 - bbox_ts[1]
        if bo_text:
            draw.text((tx, ty), bo_prefix, fill=(255, 255, 255, 200), font=font_time)
            draw.text((tx + tw_bo, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)
        else:
            draw.text((tx, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)

    # Tournament label (game-specific position & colour)
    if tournament and show_tournament:
        bbox = draw.textbbox((0, 0), tournament, font=font_tournament)
        tw = bbox[2] - bbox[0]
        top_margin = int(20 * sf)
        side_margin = int(44 * sf)
        if game == "lol":
            draw.text(((w - tw) // 2, top_margin), tournament,
                      fill=(0, 0, 0, 230), font=font_tournament)
        elif game == "tm":
            draw.text((side_margin, top_margin), tournament,
                      fill=(255, 255, 255, 230), font=font_tournament)
        else:
            draw.text((w - tw - side_margin, top_margin), tournament,
                      fill=(255, 255, 255, 230), font=font_tournament)

    out = io.BytesIO()
    canvas = canvas.convert("RGB")
    canvas.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _load_club_logo() -> Image.Image:
    return Image.open(BIG_LOGO_PATH).convert("RGBA")


def _load_background(game: str) -> Image.Image:
    """Load and resize the game-specific background. Falls back to dark solid."""
    path = GAME_BG.get(game)
    if path:
        try:
            bg = Image.open(path).convert("RGBA")
            return bg.resize((W, H), Image.LANCZOS)
        except Exception:
            log.warning("versus: background load failed for %s, using fallback", game)
    canvas = Image.new("RGBA", (W, H), (16, 16, 16, 255))
    return canvas


def _load_game_logo(game: str) -> Image.Image | None:
    """Load game logo for the bottom-left corner. Returns None on any failure.
    cs-logo is recoloured to white (like tm); lol-logo is scaled to 0.4×."""
    path = GAME_LOGO.get(game)
    if not path:
        return None
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:
        log.warning("versus: game logo load failed for %s", game)
        return None
    if game == "cs":
        # Recolour all non-transparent pixels to white
        data = img.getdata()
        new_data = []
        for item in data:
            r, g, b, a = item
            if a > 0:
                new_data.append((255, 255, 255, a))
            else:
                new_data.append(item)
        img.putdata(new_data)
    elif game == "lol":
        img = img.resize(
            (max(1, int(img.width * 0.4)), max(1, int(img.height * 0.4))),
            Image.LANCZOS,
        )
    return img


def _make_shadow(img: Image.Image, scale: float = 1.25, blur: int = 18) -> Image.Image:
    """Soft dark shadow backing for a logo or icon."""
    sw, sh = int(img.width * scale), int(img.height * scale)
    shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    pad_x, pad_y = (sw - img.width) // 2, (sh - img.height) // 2
    sd.ellipse(
        (pad_x, pad_y, sw - pad_x, sh - pad_y),
        fill=(0, 0, 0, 90),
    )
    return shadow.filter(ImageFilter.GaussianBlur(blur))


def _visible_bounds(img: Image.Image) -> tuple[int, int, int, int]:
    """Return (top, bottom, left, right) bounding box of non-transparent pixels."""
    alpha = img.getchannel("A")
    rows = [any(alpha.getpixel((x, y)) > 0 for x in range(img.width)) for y in range(img.height)]
    cols = [any(alpha.getpixel((x, y)) > 0 for y in range(img.height)) for x in range(img.width)]
    top = next(i for i, v in enumerate(rows) if v)
    bot = next(i for i, v in enumerate(reversed(rows)) if v)
    bot = img.height - 1 - bot
    left = next(i for i, v in enumerate(cols) if v)
    right = next(i for i, v in enumerate(reversed(cols)) if v)
    right = img.width - 1 - right
    return top, bot, left, right


def _crop_visible(img: Image.Image) -> Image.Image:
    """Crop to the bounding box of non-transparent pixels."""
    top, bot, left, right = _visible_bounds(img)
    return img.crop((left, top, right + 1, bot + 1))


def _make_tba_placeholder() -> Image.Image:
    """Load the TBA graphic from disk (no opponent known yet)."""
    return Image.open("resources/tba.png").convert("RGBA")
