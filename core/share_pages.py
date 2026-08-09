"""Public WhatsApp-share pages for upcoming BIG matches.

Served by the aiohttp API server on the roaringbot.casparsadenius.de
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
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp import ClientSession, ClientTimeout
from PIL import Image, ImageDraw, ImageFont

from core.config import config
from core.http_client import http_client

log = logging.getLogger("roaringbot.share")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
GAME_LABELS = {"cs": "CS", "lol": "LoL", "tm": "TM"}
API_PAGE_SIZE = 20

W, H = 1600, 800  # og:image canvas (2:1)
PAD = 80
BIG_LOGO_PATH = "resources/big.png"

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
    real visitors are redirected via meta refresh + JS."""
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
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def handle_share_slug_redirect(request: web.Request) -> web.Response:
    """Compatibility: old slug-based URLs 301 to their id-based URL, so
    already-shared messages keep working (WhatsApp previews too)."""
    match = await _find_match_by_slug(request.match_info["slug"])
    if match is None:
        return web.Response(status=404)
    target = _share_url(match["id"])
    if request.path.endswith("/image.png"):
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
        label = dt.strftime("%a %d %b")
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
    return f"{config.share_base_url}/share/{match_id}/"


def _image_url(match_id: int) -> str:
    return f"{config.share_base_url}/share/{match_id}/image.png"


def _build_card(m: dict) -> str:
    """One list-page card; the share button posts the match info as plain
    text plus the match-page URL (WhatsApp renders the preview from the og:
    tags on that page)."""
    title, bo_line, tournament, time_str, _ = _match_info(m)
    share_url = _share_url(m["id"])
    share_text = " · ".join(
        part for part in (title, bo_line, tournament, time_str) if part
    )

    return f"""<div class="card">
<img class="versus-img" src="/share/{m['id']}/image.png" alt="{_esc(title)}">
<div class="info">{_esc(title)} &middot; {_esc(bo_line)}</div>
<div class="info">{_esc(tournament)} &middot; {_esc(time_str)}</div>
<button class="share-btn" type="button" data-url="{_esc(share_url)}" data-text="{_esc(share_text)}">Share to WhatsApp</button>
<div class="share-msg"></div>
</div>"""


def _match_page_html(m: dict) -> str:
    """Match page: og: tags (WhatsApp preview) + redirect to wannspieltbig."""
    title, bo_line, tournament, time_str, match_url = _match_info(m)
    og_title = f"⚔️ {title} · {time_str} · {bo_line}"
    og_image = _image_url(m["id"])
    share_url = _share_url(m["id"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)} – {_esc(time_str)}</title>
<meta property="og:title" content="{_esc(og_title)}">
<meta property="og:description" content="{_esc(tournament)} – Kickoff {_esc(time_str)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{_esc(share_url)}">
<meta property="og:image" content="{_esc(og_image)}">
<meta property="og:image:width" content="1600">
<meta property="og:image:height" content="800">
<meta name="twitter:card" content="summary_large_image">
<meta http-equiv="refresh" content="0;url={_esc(match_url)}">
<script>location.replace({json.dumps(match_url)});</script>
</head>
<body style="margin:0;background:#0f0f0f;color:#ccc;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh">
<p style="text-align:center">Opening match page&hellip;<br>
<a href="{_esc(match_url)}" style="color:#6c9bcf">{_esc(match_url)}</a></p>
</body>
</html>"""


# ── Image composition ───────────────────────────────────────────────────────

# Built PNGs, keyed by slug → (content signature, bytes). The signature
# changes on reschedule/opponent swap, so a stale cache entry is rebuilt.
# Without this, every request (e.g. a WhatsApp crawl after a restart) would
# re-download the opponent logo and risk timing out the crawler.
_image_cache: dict[str, tuple[str, bytes]] = {}


def _image_signature(m: dict) -> str:
    """Content key for the image cache: rebuild when kickoff, tournament or
    logos change."""
    lineup_a = m.get("lineup_a") or {}
    lineup_b = m.get("lineup_b") or {}
    return "|".join(
        [
            m.get("slug") or "",
            m.get("first_map_at") or "",
            (m.get("tournament") or {}).get("name") or "",
            (lineup_a.get("team_logo_url") or "").strip(),
            (lineup_b.get("team_logo_url") or "").strip(),
        ]
    )


async def _ensure_image(match: dict) -> bytes:
    """Return the cached versus PNG for a match, building it if stale/missing."""
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


async def _build_versus_image(m: dict) -> bytes:
    """Composite club logo + opponent logo onto a dark 2:1 canvas, with a
    match-info line (game · BO · kickoff) at the bottom."""
    lineup_a = m.get("lineup_a") or {}
    lineup_b = m.get("lineup_b") or {}
    opp_logo = (lineup_b.get("team_logo_url") or "").strip()
    big_logo = (lineup_a.get("team_logo_url") or "").strip()

    canvas = Image.new("RGBA", (W, H), (16, 16, 16, 255))

    if big_logo:
        try:
            big = await _fetch_logo(big_logo)
        except Exception:
            log.warning("share: BIG logo fetch failed, using local big.png")
            big = _load_club_logo()
    else:
        big = _load_club_logo()

    if opp_logo:
        try:
            opponent = await _fetch_logo(opp_logo)
        except Exception:
            log.warning("share: opponent logo fetch failed, using TBA placeholder")
            opponent = _make_tba_placeholder()
    else:
        opponent = _make_tba_placeholder()

    text_h = 160  # bottom band: match-info line + tournament line
    half = W // 2
    box_w, box_h = half - 2 * PAD, H - 2 * PAD - text_h

    def fit(img):
        r = min(box_w / img.width, box_h / img.height)
        return img.resize(
            (max(1, int(img.width * r)), max(1, int(img.height * r))),
            Image.LANCZOS,
        )

    big_f, opp_f = fit(big), fit(opponent)
    canvas.paste(
        big_f,
        (PAD + (box_w - big_f.width) // 2, (H - text_h - big_f.height) // 2),
        big_f,
    )
    canvas.paste(
        opp_f,
        (
            half + PAD + (box_w - opp_f.width) // 2,
            (H - text_h - opp_f.height) // 2,
        ),
        opp_f,
    )

    # Match info at the bottom: "CS · BO3 · Today 17:10", tournament below.
    _, bo_line, tournament, time_str, _ = _match_info(m)
    draw = ImageDraw.Draw(canvas)
    band_top = H - text_h
    if tournament:
        _draw_line(draw, f"{bo_line} · {time_str}", band_top + 50, 44, 235)
        _draw_line(draw, tournament, band_top + 112, 34, 175)
    else:
        _draw_line(draw, f"{bo_line} · {time_str}", band_top + text_h // 2, 44, 235)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


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


def _draw_line(draw: ImageDraw.ImageDraw, text: str, y_center: int,
               font_size: int, alpha: int) -> None:
    """Draw one centered text line on the canvas at the given center y."""
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size
        )
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((W - tw) // 2 - bbox[0], y_center - th // 2 - bbox[1]),
        text,
        fill=(255, 255, 255, alpha),
        font=font,
    )


def _make_tba_placeholder() -> Image.Image:
    """512×512 transparent PNG with 'TBA' text (no opponent known yet)."""
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72
        )
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "TBA", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((512 - tw) // 2, (512 - th) // 2),
        "TBA",
        fill=(255, 255, 255, 180),
        font=font,
    )
    return img


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
.share-btn{display:block;width:100%;padding:14px;background:#25D366;color:#000;border:none;border-radius:10px;font-size:17px;font-weight:700;cursor:pointer;touch-action:manipulation}
.share-btn:active{background:#1da851}
.share-msg{display:none;color:#f4a261;font-size:13px;margin-top:8px;word-break:break-word}
</style>
</head>
<body>
<h1>Upcoming Matches</h1>
<div class="cards">
<!-- CARDS -->
</div>
<script>
(function(){
function showMsg(btn,msg){
  const el=btn.parentElement.querySelector('.share-msg');
  el.textContent=msg;
  el.style.display='block';
  setTimeout(()=>{el.style.display='none'},4000);
}

document.querySelectorAll('.share-btn').forEach(btn=>{
  btn.addEventListener('click',async()=>{
    // Share the match info as plain text plus the URL; WhatsApp renders
    // the link preview (versus image, match info) from the og: tags on
    // the match page.
    const msg=btn.dataset.text+'\\n'+btn.dataset.url;
    try{
      if(navigator.share){
        await navigator.share({text:msg});
        return;
      }
    }catch(e){
      if(e.name==='AbortError') return;
    }
    try{
      await navigator.clipboard.writeText(msg);
      showMsg(btn,'Link copied — paste it in WhatsApp');
    }catch(e){}
  });
});
})();
</script>
</body>
</html>"""
