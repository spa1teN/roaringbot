"""Lightweight aiohttp API server for dashboard feedback management.

Shares the bot's asyncio event loop. Exposes read/write endpoints for
feedback entries — the dashboard proxies to them. See DATA_INTERFACE.md.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from core.share_pages import (
    handle_share_image,
    handle_share_list,
    handle_share_match,
    handle_share_next_match,
    handle_share_slug_redirect,
)

log = logging.getLogger("roaringbot.api")

# ── Helpers ──────────────────────────────────────────────────────────────

def _guild_avatar_url(guild: Any) -> str | None:
    try:
        if guild.icon:
            return str(guild.icon.url)
    except Exception:
        pass
    return None


def _user_info(bot: Any, user_id: int) -> tuple[str | None, str | None]:
    """Look up user name + avatar URL from Discord cache. Returns (None, None) on miss."""
    try:
        user = bot.get_user(user_id)
        if user is None:
            return None, None
        name = user.display_name or user.name
        avatar = str(user.avatar.url) if user.avatar else str(user.default_avatar.url)
        return name, avatar
    except Exception:
        return None, None


def _enrich_entry(entry: dict[str, Any], bot: Any) -> dict[str, Any]:
    """Add guild_name, guild_avatar_url, user_name, user_avatar_url to a DB row dict."""
    out = dict(entry)

    # Convert datetime to ISO string
    if out.get("created_at"):
        out["created_at"] = out["created_at"].isoformat()

    # Guild enrichment
    guild_id = entry.get("guild_id")
    if guild_id:
        guild = bot.get_guild(guild_id)
        if guild:
            out.setdefault("guild_name", guild.name)
            out["guild_avatar_url"] = _guild_avatar_url(guild)
        else:
            out.setdefault("guild_name", "Unknown")
            out["guild_avatar_url"] = None

    # User enrichment (only if not anonymous)
    if not entry.get("is_anonymous") and entry.get("user_id"):
        name, avatar = _user_info(bot, entry["user_id"])
        out["user_name"] = name
        out["user_avatar_url"] = avatar
    else:
        out["user_name"] = None
        out["user_avatar_url"] = None

    return out


# ── Route handlers ───────────────────────────────────────────────────────

async def handle_feedback_list(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    db = bot.db
    if db is None or not db.is_connected:
        return web.json_response({"error": "DB not connected"}, status=503)

    guild_id_str = request.query.get("guild_id")
    guild_id = int(guild_id_str) if guild_id_str else None
    try:
        rows = await db.feedback.list_feedback(guild_id=guild_id)
        enriched = [_enrich_entry(dict(r), bot) for r in rows]
        return web.json_response(enriched)
    except Exception:
        log.exception("GET /api/feedback failed")
        return web.json_response({"error": "query failed"}, status=500)


async def handle_mark_read(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    db = bot.db
    if db is None or not db.is_connected:
        return web.json_response({"error": "DB not connected"}, status=503)

    try:
        feedback_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "invalid id"}, status=400)

    try:
        ok = await db.feedback.mark_read(feedback_id)
        return web.json_response({"ok": ok})
    except Exception:
        log.exception("PATCH /api/feedback/{id}/read failed")
        return web.json_response({"ok": False, "error": "update failed"}, status=500)


async def handle_set_status(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    db = bot.db
    if db is None or not db.is_connected:
        return web.json_response({"error": "DB not connected"}, status=503)

    try:
        feedback_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "invalid id"}, status=400)

    try:
        body = await request.json()
        status = body.get("status", "")
    except Exception:
        status = request.query.get("status", "")

    if not status:
        return web.json_response({"error": "missing status"}, status=400)

    try:
        ok = await db.feedback.set_status(feedback_id, status)
        return web.json_response({"ok": ok})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("PATCH /api/feedback/{id}/status failed")
        return web.json_response({"ok": False, "error": "update failed"}, status=500)


async def handle_set_note(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    db = bot.db
    if db is None or not db.is_connected:
        return web.json_response({"error": "DB not connected"}, status=503)

    try:
        feedback_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "invalid id"}, status=400)

    try:
        body = await request.json()
        note = body.get("note", "")
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)

    try:
        ok = await db.feedback.set_admin_note(feedback_id, note)
        return web.json_response({"ok": ok})
    except Exception:
        log.exception("PATCH /api/feedback/{id}/note failed")
        return web.json_response({"ok": False, "error": "update failed"}, status=500)


async def handle_unread_count(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    db = bot.db
    if db is None or not db.is_connected:
        return web.json_response({"error": "DB not connected"}, status=503)

    guild_id_str = request.query.get("guild_id")
    if not guild_id_str:
        return web.json_response({"error": "missing guild_id"}, status=400)

    try:
        guild_id = int(guild_id_str)
        count = await db.feedback.get_unread_count(guild_id)
        return web.json_response({"count": count})
    except Exception:
        log.exception("GET /api/feedback/unread-count failed")
        return web.json_response({"count": 0, "error": "query failed"}, status=500)


async def handle_bot_avatar(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    try:
        url = str(bot.user.avatar.url) if bot.user and bot.user.avatar else None
        return web.json_response({"bot_avatar_url": url})
    except Exception:
        return web.json_response({"bot_avatar_url": None})


# ── App factory ──────────────────────────────────────────────────────────

def create_app(bot: Any) -> web.Application:
    app = web.Application()
    app["bot"] = bot

    app.router.add_get("/api/feedback", handle_feedback_list)
    app.router.add_patch("/api/feedback/{id}/read", handle_mark_read)
    app.router.add_patch("/api/feedback/{id}/status", handle_set_status)
    app.router.add_patch("/api/feedback/{id}/note", handle_set_note)
    app.router.add_get("/api/feedback/unread-count", handle_unread_count)
    app.router.add_get("/api/bot/avatar", handle_bot_avatar)

    # Public WhatsApp-share pages — canonical URLs are short:
    #   bot.wannspieltbig.de/           → match list
    #   bot.wannspieltbig.de/{id}        → match page (og: tags + JS redirect)
    #   bot.wannspieltbig.de/{id}/image.jpg → versus thumbnail
    # Old /share/… and /share-match/ routes are kept for backward compat
    # with already-shared WhatsApp messages.
    app.router.add_get("/", handle_share_list)
    app.router.add_get(r"/{match_id:\d+}", handle_share_match)
    app.router.add_get(r"/{match_id:\d+}/image.jpg", handle_share_image)
    # Legacy /share/… URLs (still served, canonical ones are above)
    app.router.add_get("/share/", handle_share_list)
    app.router.add_get(r"/share/{match_id:\d+}/", handle_share_match)
    app.router.add_get(r"/share/{match_id:\d+}/image.jpg", handle_share_image)
    # Old slug-based URLs keep working (301 to the id-based URL)
    app.router.add_get("/share/{slug}/", handle_share_slug_redirect)
    app.router.add_get("/share/{slug}/image.jpg", handle_share_slug_redirect)
    # Old-URL alias of the overview list (same page as /)
    app.router.add_get("/share-match/", handle_share_next_match)

    return app


async def start_api_server(bot: Any, port: int = 8080) -> web.AppRunner:
    """Start the API server on the bot's event loop. Returns the runner for cleanup."""
    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"API server listening on port {port}")
    return runner
