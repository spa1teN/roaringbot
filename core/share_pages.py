"""Public WhatsApp-share pages for upcoming BIG matches.

Served by the aiohttp API server on the bot.wannspieltbig.de
subdomain (nginx proxies only /share/* — the feedback API stays internal).

Flow: the operator opens /share/, taps "Share to WhatsApp" on a match →
only the match-page URL is shared → WhatsApp generates a link preview from
the page's og: tags (versus image via og:image) → tapping the link opens
the page, which redirects to the specific match page on wannspieltbig.de.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp import ClientSession, ClientTimeout
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.config import config
from core.http_client import http_client

log = logging.getLogger("roaringbot.share")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
GAME_LABELS = {"cs": "CS", "lol": "LoL", "tm": "TM"}
API_PAGE_SIZE = 20

W, H = 1600, 800  # og:image canvas (2:1)
PAD = 80
BIG_LOGO_PATH = "resources/big_square.png"

GAME_BG = {"cs": "resources/cs-bg.jpg", "lol": "resources/lol-bg.jpg", "tm": "resources/tm-bg.jpg"}
GAME_LOGO = {"cs": "resources/cs-logo.png", "lol": "resources/lol-logo.png", "tm": "resources/tm-logo.png"}

# ── Handlers ────────────────────────────────────────────────────────────────


async def handle_share_list(request: web.Request) -> web.Response:
    """List page: one card per upcoming match with a Share-to-WhatsApp button."""
    matches = await _fetch_upcoming_matches()
    # Pre-warm versus images in the background, so the first WhatsApp crawl
    # of a match URL hits a warm PNG instead of a slow cold logo fetch
    # (WhatsApp's crawler drops the image if the fetch takes too long).
    for m in matches:
        if m.get("slug"):
            asyncio.create_task(_warm_image(m))
    cards = "\n".join(_build_card(m) for m in matches)
    html = _LIST_HTML.replace("<!-- CARDS -->", cards)
    return web.Response(text=html, content_type="text/html")


async def handle_share_match(request: web.Request) -> web.Response:
    """Match page: og: tags for the WhatsApp preview, then redirect to the
    wannspieltbig match page. Crawlers parse the og: tags from this response;
    real visitors are redirected via JS (no meta refresh — WhatsApp follows it)."""
    match = await _find_match_by_id(int(request.match_info["match_id"]))
    if match is None:
        return web.Response(text="Match not found", status=404)
    return web.Response(
        text=_match_page_html(match), content_type="text/html"
    )


async def handle_share_image(request: web.Request) -> web.Response:
    """Versus PNG for og:image — club logo left, opponent right, dark 2:1."""
    match = await _find_match_by_id(int(request.match_info["match_id"]))
    if match is None:
        return web.Response(status=404)
    try:
        png = await _ensure_image(match)
    except Exception:
        log.exception("share: image build failed for %s", match.get("slug"))
        return web.Response(status=500)
    return web.Response(
        body=png,
        content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def handle_share_slug_redirect(request: web.Request) -> web.Response:
    """Compatibility: old slug-based URLs 301 to their id-based URL, so
    already-shared messages keep working (WhatsApp previews too)."""
    match = await _find_match_by_slug(request.match_info["slug"])
    if match is None:
        return web.Response(status=404)
    target = _share_url(match["id"])
    if request.path.endswith("/image.jpg"):
        target = _image_url(match["id"])
    return web.Response(status=301, headers={"Location": target})


# /share-match/ serves the same overview as /share/: one card per upcoming
# match, each sharing its /share/{slug}/ page (WhatsApp embed picture via
# the og: tags on that page). Kept under the old URL the operator knows.
handle_share_next_match = handle_share_list


# ── Data ────────────────────────────────────────────────────────────────────


async def _fetch_matches(upcoming_only: bool) -> list:
    """Fetch non-cancelled matches from wannspieltbig, sorted by kickoff."""
    url = config.esports_api_url.rstrip("/") + f"/?limit={API_PAGE_SIZE}"
    try:
        resp = await http_client.get(url)
        if resp.status != 200:
            return []
        data = await resp.json()
    except Exception:
        log.exception("share: match fetch failed")
        return []

    matches = [m for m in data.get("results", []) if not m.get("cancelled")]
    if upcoming_only:
        matches = [m for m in matches if not m.get("has_ended")]
    matches.sort(
        key=lambda m: _parse_match_time(m.get("first_map_at", ""))[0]
        or datetime.max.replace(tzinfo=BERLIN_TZ)
    )
    return matches


async def _fetch_upcoming_matches() -> list:
    """Upcoming non-cancelled matches, sorted by kickoff."""
    return await _fetch_matches(upcoming_only=True)


async def _fetch_all_matches() -> list:
    """Full list (incl. ended matches) — /share/{id}/ keeps working for past
    matches, so previously shared games can be re-shared."""
    return await _fetch_matches(upcoming_only=False)


async def _find_match_by_id(match_id: int) -> Optional[dict]:
    for m in await _fetch_all_matches():
        if m.get("id") == match_id:
            return m
    return None


async def _find_match_by_slug(slug: str) -> Optional[dict]:
    for m in await _fetch_all_matches():
        if m.get("slug") == slug:
            return m
    return None


def _parse_match_time(first_map_at: str):
    """Parse '2026-08-09T14:45:00+02:00' → (Berlin datetime, display label)."""
    try:
        ts = re.sub(r"[+-]\d{2}:\d{2}$", "", first_map_at)
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=BERLIN_TZ)
    except (ValueError, TypeError):
        return None, first_map_at or ""

    today = datetime.now(BERLIN_TZ).date()
    d = dt.date()
    if d == today:
        label = "Today"
    elif d == today + timedelta(days=1):
        label = "Tomorrow"
    else:
        label = dt.strftime("%d %b")
    return dt, f"{label} {dt.strftime('%H:%M')}"


# ── HTML builders ───────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    """Escape text for safe inclusion in an HTML attribute."""
    return (text or "").replace("&", "&amp;").replace('"', "&quot;")


def _match_info(m: dict) -> tuple:
    """Extract display info from an API match dict."""
    lineup_b = m.get("lineup_b") or {}
    team_b = (lineup_b.get("team") or {}).get("name")
    lineup_a = m.get("lineup_a") or {}
    team_a = (lineup_a.get("team") or {}).get("name") or "BIG"

    game = (m.get("game") or "").lower()
    game_label = GAME_LABELS.get(game, game.upper())
    bo = f"BO{m['bestof']}" if m.get("bestof") else ""
    tournament = (m.get("tournament") or {}).get("name") or ""
    match_url = m.get("html_detail_url") or "https://wannspieltbig.de/"
    _, time_str = _parse_match_time(m.get("first_map_at", ""))

    title = f"{team_a} vs. {team_b}" if team_b else f"{team_a} vs. TBA"
    bo_line = f"{game_label} · {bo}" if bo else game_label
    return title, bo_line, tournament, time_str, match_url


def _share_url(match_id: int) -> str:
    return f"{config.share_base_url}/{match_id}"


def _image_url(match_id: int) -> str:
    return f"{config.share_base_url}/{match_id}/image.jpg"


def _build_card(m: dict) -> str:
    """One list-page card; the share button posts the match info as plain
    text plus the match-page URL (WhatsApp renders the preview from the og:
    tags on that page)."""
    title, bo_line, tournament, time_str, _ = _match_info(m)
    share_url = _share_url(m["id"])
    # WhatsApp SVG icon (official brand icon, simplified)
    wa_icon = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413z"/></svg>'
    # Clipboard copy icon
    copy_icon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    check_icon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
    return f"""<div class="card">
<img class="versus-img" src="/{m['id']}/image.jpg" alt="{_esc(title)}">
<div class="info">{_esc(title)} &middot; {_esc(bo_line)}</div>
<div class="info">{_esc(tournament)} &middot; {_esc(time_str)}</div>
<div class="btn-row">
<button class="btn-wa" type="button" data-url="{_esc(share_url)}">{wa_icon} WhatsApp</button>
<button class="btn-copy" type="button" data-url="{_esc(share_url)}" data-wa-icon="{_esc(wa_icon)}" data-copy-icon="{_esc(copy_icon)}" data-check-icon="{_esc(check_icon)}">{copy_icon} Copy</button>
</div>
</div>"""


def _match_page_html(m: dict) -> str:
    """Match page: og: tags (WhatsApp preview) + redirect to wannspieltbig."""
    title, bo_line, tournament, time_str, match_url = _match_info(m)
    og_title = f"{title} · {time_str} · {bo_line}"
    og_image = _image_url(m["id"])
    share_url = _share_url(m["id"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)} – {_esc(time_str)}</title>
<meta property="og:title" content="{_esc(og_title)}">
<meta property="og:description" content="{_esc(tournament)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{_esc(share_url)}">
<meta property="og:image" content="{_esc(og_image)}">
<meta property="og:image:width" content="1600">
<meta property="og:image:height" content="800">
<meta name="twitter:card" content="summary_large_image">
<script>location.replace({json.dumps(match_url)});</script>
</head>
<body style="margin:0;background:#0f0f0f;color:#ccc;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh">
<p style="text-align:center">Opening match page&hellip;<br>
<a href="{_esc(match_url)}" style="color:#6c9bcf">{_esc(match_url)}</a></p>
</body>
</html>"""


# ── Image composition ───────────────────────────────────────────────────────

# Built JPEGs, keyed by slug → (content signature, bytes). The signature
# changes on reschedule/opponent swap, so a stale cache entry is rebuilt.
# Without this, every request (e.g. a WhatsApp crawl after a restart) would
# re-download the opponent logo and risk timing out the crawler.
# JPEG is used instead of PNG to stay under WhatsApp's ~300 KB og:image limit.
_image_cache: dict[str, tuple[str, bytes]] = {}


def _image_signature(m: dict) -> str:
    """Content key for the image cache: rebuild when kickoff, tournament,
    opponent logo, or current Berlin date changes.  Including today's date
    ensures the "Today"/"Tomorrow" label baked into the JPEG is always
    correct across midnight (BIG logo is always local big_square.png)."""
    lineup_b = m.get("lineup_b") or {}
    today = datetime.now(BERLIN_TZ).date().isoformat()
    return "|".join(
        [
            m.get("slug") or "",
            m.get("first_map_at") or "",
            (m.get("tournament") or {}).get("name") or "",
            (lineup_b.get("team_logo_url") or "").strip(),
            today,
        ]
    )


async def _ensure_image(match: dict) -> bytes:
    """Return the cached versus JPEG for a match, building it if stale/missing."""
    slug = match.get("slug") or ""
    sig = _image_signature(match)
    cached = _image_cache.get(slug)
    if cached and cached[0] == sig:
        return cached[1]
    png = await _build_versus_image(match)
    _image_cache[slug] = (sig, png)
    if len(_image_cache) > 50:  # bound memory; newest match stays anyway
        _image_cache.pop(next(iter(_image_cache)))
    return png


async def _warm_image(match: dict) -> None:
    """Background pre-warm; failures are logged, never surfaced."""
    try:
        await _ensure_image(match)
    except Exception:
        log.warning("share: pre-warm image build failed for %s", match.get("slug"))


def compose_versus_image(opponent_png: bytes, *, game: str, tournament: str,
                         bo_text: str, time_str: str,
                         w: int = W, h: int = H) -> bytes:
    """Composite versus thumbnail: game-specific background, BIG's square
    logo, opponent logo, game logo bottom-left, match info on dark
    triangle overlays in the corners. Output is JPEG quality 85.

    Dimensions default to 2:1 (1600×800); pass h=400 for 4:1 event covers.
    This is the single canonical composition used by both the WhatsApp share
    pages and Discord reminders/event covers."""
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

    # Text measurement
    bo_prefix = f"{bo_text} · " if bo_text else ""
    bbox_bo = draw.textbbox((0, 0), bo_prefix, font=font_time)
    bbox_ts = draw.textbbox((0, 0), time_str, font=font_time_bold)
    th = max(bbox_bo[3] - bbox_bo[1], bbox_ts[3] - bbox_ts[1])
    tw_bo = bbox_bo[2] - bbox_bo[0]
    tw_ts = bbox_ts[2] - bbox_ts[0]

    # Game logo + BO/time bottom-left
    game_logo = _load_game_logo(game)
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
            ty = centre_y - th // 2 - bbox_ts[1]
            gl_y = ty
        else:
            scale_mult = 0.80
            gl_scale_val = text_h / gl.height * scale_mult
            gl_w, gl_h = int(gl.width * gl_scale_val), int(gl.height * gl_scale_val)
            gl = gl.resize((gl_w, gl_h), Image.LANCZOS)
            gl_x = int(36 * sf)
            centre_y = band_top + text_h // 2 + optical_offset
            ty = centre_y - th // 2 - bbox_ts[1]
            gl_y = band_top + (text_h - gl_h) // 2 + optical_offset
        canvas.paste(gl, (gl_x, gl_y), gl)

        tx = gl_x + gl_w + int(24 * sf)
        if bo_text:
            draw.text((tx, ty), bo_prefix, fill=(255, 255, 255, 200), font=font_time)
            draw.text((tx + tw_bo, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)
        else:
            draw.text((tx, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)
    else:
        total_w = tw_bo + tw_ts
        tx = (w // 2 - total_w) // 2 - bbox_bo[0]
        ty = h - dy // 2 - th // 2 - bbox_ts[1]
        if bo_text:
            draw.text((tx, ty), bo_prefix, fill=(255, 255, 255, 200), font=font_time)
            draw.text((tx + tw_bo, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)
        else:
            draw.text((tx, ty), time_str, fill=(255, 255, 255, 240), font=font_time_bold)

    # Tournament label (game-specific position & colour)
    if tournament:
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


async def _build_versus_image(m: dict) -> bytes:
    """Fetch opponent logo and delegate to compose_versus_image for the
    shared composition (game background, both logos, overlays, text)."""
    lineup_b = m.get("lineup_b") or {}
    opp_logo = (lineup_b.get("team_logo_url") or "").strip()
    game = (m.get("game") or "").lower()
    bo_text = f"BO{m['bestof']}" if m.get("bestof") else ""
    _, _, tournament, time_str, _ = _match_info(m)

    # Fetch opponent logo bytes (or TBA placeholder)
    if opp_logo:
        try:
            opponent_img = await _fetch_logo(opp_logo)
            opponent_img = _crop_visible(opponent_img)
            buf = io.BytesIO()
            opponent_img.save(buf, format="PNG")
            opponent_bytes = buf.getvalue()
        except Exception:
            log.warning("share: opponent logo fetch failed, using TBA placeholder")
            buf = io.BytesIO()
            _make_tba_placeholder().save(buf, format="PNG")
            opponent_bytes = buf.getvalue()
    else:
        buf = io.BytesIO()
        _make_tba_placeholder().save(buf, format="PNG")
        opponent_bytes = buf.getvalue()

    return compose_versus_image(
        opponent_bytes,
        game=game,
        tournament=tournament,
        bo_text=bo_text,
        time_str=time_str,
    )


async def _fetch_logo(url: str) -> Image.Image:
    """Download a logo; direct fetch first, weserv proxy as fallback.

    imgur (BIG's logos) blocks weserv but serves direct requests; HLTV CDN
    blocks direct requests from server IPs but works through weserv.
    Uses its own short-timeout session — http_client would retry for
    ~60 s, far too slow for a WhatsApp crawler fetching og:image.
    """
    proxy_url = (
        "https://images.weserv.nl/?url="
        + urllib.parse.quote(re.sub(r"^https?://", "", url), safe="")
        + "&w=400"
    )
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    timeout = ClientTimeout(total=10)
    async with ClientSession(timeout=timeout) as session:
        for attempt in (url, proxy_url):
            try:
                async with session.get(attempt, headers=headers) as resp:
                    if resp.status == 200:
                        return Image.open(io.BytesIO(await resp.read())).convert(
                            "RGBA"
                        )
            except Exception:
                continue
    raise RuntimeError(f"logo fetch failed: {url}")


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
            log.warning("share: background load failed for %s, using fallback", game)
    canvas = Image.new("RGBA", (W, H), (16, 16, 16, 255))
    return canvas


def _load_game_logo(game: str) -> Image.Image | None:
    """Load game logo for the bottom-left corner. Returns None on any failure.
    cs-logo is recoloured to white (like tm); lol-logo is scaled to 0.7×."""
    path = GAME_LOGO.get(game)
    if not path:
        return None
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:
        log.warning("share: game logo load failed for %s", game)
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


# ── List page ───────────────────────────────────────────────────────────────

_LIST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BIG Matches</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f0f0f;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;min-height:100vh;padding:16px}
h1{text-align:center;font-size:20px;margin:8px 0 16px;color:#fff}
.cards{display:flex;flex-direction:column;gap:16px;max-width:420px;margin:0 auto}
.card{background:#1a1a1a;border-radius:12px;padding:16px;text-align:center}
.versus-img{width:100%;border-radius:8px;margin-bottom:12px;background:#222}
.info{color:#ccc;font-size:14px;margin-bottom:4px}
.btn-row{display:flex;gap:10px;margin-top:4px}
.btn-row button{flex:1;display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 16px;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer;touch-action:manipulation}
.btn-wa{background:#25D366;color:#000}
.btn-wa:active{background:#1da851}
.btn-copy{background:#333;color:#e0e0e0}
.btn-copy:active{background:#444}
.btn-copy.copied{background:#2a6b3a}
.btn-row svg{flex-shrink:0}
</style>
</head>
<body>
<h1>Upcoming Matches</h1>
<div class="cards">
<!-- CARDS -->
</div>
<script>
(function(){
// WhatsApp button — open wa.me with the URL pre-filled
document.querySelectorAll('.btn-wa').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const waUrl='https://wa.me/?text='+encodeURIComponent(btn.dataset.url);
    window.open(waUrl,'_blank');
  });
});
// Copy button — copy URL to clipboard with visual feedback
document.querySelectorAll('.btn-copy').forEach(btn=>{
  btn.addEventListener('click',async()=>{
    try{
      await navigator.clipboard.writeText(btn.dataset.url);
      btn.innerHTML=btn.dataset.checkIcon+' Copied';
      btn.classList.add('copied');
      setTimeout(()=>{
        btn.innerHTML=btn.dataset.copyIcon+' Copy';
        btn.classList.remove('copied');
      },2000);
    }catch(e){
      // Fallback for older browsers / non-HTTPS
      const ta=document.createElement('textarea');
      ta.value=btn.dataset.url;ta.style.position='fixed';ta.style.opacity='0';
      document.body.appendChild(ta);ta.select();
      document.execCommand('copy');document.body.removeChild(ta);
      btn.innerHTML=btn.dataset.checkIcon+' Copied';
      btn.classList.add('copied');
      setTimeout(()=>{
        btn.innerHTML=btn.dataset.copyIcon+' Copy';
        btn.classList.remove('copied');
      },2000);
    }
  });
});
})();
</script>
</body>
</html>"""
