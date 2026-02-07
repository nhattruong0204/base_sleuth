"""Interactive Telegram bot commands and inline keyboard menus.

Exposes all Base Sleuth features as interactive buttons:

Commands
--------
/start, /menu  — Main dashboard with inline keyboard
/status         — Bot status (uptime, loops, backoff)
/stats          — Database statistics (tokens, alerts, scoring)
/gems           — Recent gems that passed scoring threshold
/top            — Top scored tokens
/scan           — Force scan (firehose / champagne / breakout)
/config         — View current filter configuration
/help           — Show all available commands

Inline keyboard callbacks handle sub-menus and actions.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import structlog
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from sqlalchemy import func, select

from .models import Token, TokenContext, TokenMetrics

if TYPE_CHECKING:
    from .main import Tracker

logger = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────
# Keyboard layouts
# ──────────────────────────────────────────────────────────────────

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the main dashboard inline keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("📈 Stats", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("💎 Recent Gems", callback_data="gems"),
            InlineKeyboardButton("🏆 Top Tokens", callback_data="top"),
        ],
        [
            InlineKeyboardButton("🔍 Force Scan", callback_data="scan_menu"),
            InlineKeyboardButton("⚙️ Config", callback_data="config"),
        ],
        [
            InlineKeyboardButton("🍾 Champagne", callback_data="champagne"),
            InlineKeyboardButton("📈 Breakouts", callback_data="breakouts"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ])


def _scan_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the scan sub-menu inline keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔥 Firehose Now", callback_data="scan_firehose"),
            InlineKeyboardButton("🍾 Champagne Now", callback_data="scan_champagne"),
        ],
        [
            InlineKeyboardButton("📈 Breakout Now", callback_data="scan_breakout"),
            InlineKeyboardButton("⚡ Eval Now", callback_data="scan_eval"),
        ],
        [
            InlineKeyboardButton("🔄 Scan All", callback_data="scan_all"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="menu"),
        ],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    """Single 'Back to Menu' button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")],
    ])


# ──────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start and /menu — show main dashboard."""
    await update.message.reply_text(
        "🔍 <b>Base Sleuth Dashboard</b>\n\n"
        "Autonomous hidden-gem discovery agent for Base blockchain.\n"
        "Choose an action below:",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_menu_keyboard(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    tracker: Tracker = context.bot_data["tracker"]
    text = await _build_status_text(tracker)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=_back_keyboard(),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats command."""
    tracker: Tracker = context.bot_data["tracker"]
    text = await _build_stats_text(tracker)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=_back_keyboard(),
    )


async def cmd_gems(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /gems command."""
    tracker: Tracker = context.bot_data["tracker"]
    text = await _build_gems_text(tracker)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=_back_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /top command."""
    tracker: Tracker = context.bot_data["tracker"]
    text = await _build_top_text(tracker)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=_back_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /scan command — show scan sub-menu."""
    await update.message.reply_text(
        "🔍 <b>Force Scan</b>\n\nChoose which scanner to trigger:",
        parse_mode=ParseMode.HTML,
        reply_markup=_scan_menu_keyboard(),
    )


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config command."""
    tracker: Tracker = context.bot_data["tracker"]
    text = _build_config_text(tracker)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=_back_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    text = _build_help_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=_main_menu_keyboard(),
    )


# ──────────────────────────────────────────────────────────────────
# Callback query handler (inline button presses)
# ──────────────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all inline keyboard button presses."""
    query = update.callback_query
    await query.answer()  # Acknowledge button press

    tracker: Tracker = context.bot_data["tracker"]
    data = query.data

    if data == "menu":
        await query.edit_message_text(
            "🔍 <b>Base Sleuth Dashboard</b>\n\n"
            "Autonomous hidden-gem discovery agent for Base blockchain.\n"
            "Choose an action below:",
            parse_mode=ParseMode.HTML,
            reply_markup=_main_menu_keyboard(),
        )

    elif data == "status":
        text = await _build_status_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=_back_keyboard(),
        )

    elif data == "stats":
        text = await _build_stats_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=_back_keyboard(),
        )

    elif data == "gems":
        text = await _build_gems_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )

    elif data == "top":
        text = await _build_top_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )

    elif data == "scan_menu":
        await query.edit_message_text(
            "🔍 <b>Force Scan</b>\n\nChoose which scanner to trigger:",
            parse_mode=ParseMode.HTML,
            reply_markup=_scan_menu_keyboard(),
        )

    elif data == "scan_firehose":
        await _handle_force_scan(query, tracker, "firehose")

    elif data == "scan_champagne":
        await _handle_force_scan(query, tracker, "champagne")

    elif data == "scan_breakout":
        await _handle_force_scan(query, tracker, "breakout")

    elif data == "scan_eval":
        await _handle_force_scan(query, tracker, "eval")

    elif data == "scan_all":
        await _handle_force_scan(query, tracker, "all")

    elif data == "config":
        text = _build_config_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=_back_keyboard(),
        )

    elif data == "champagne":
        text = await _build_champagne_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )

    elif data == "breakouts":
        text = await _build_breakouts_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )

    elif data == "help":
        text = _build_help_text()
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=_main_menu_keyboard(),
        )


# ──────────────────────────────────────────────────────────────────
# Text builders
# ──────────────────────────────────────────────────────────────────

async def _build_status_text(tracker: Tracker) -> str:
    """Build bot status text with uptime, loop health, backoff."""
    uptime_s = time.monotonic() - tracker._start_time
    hours = int(uptime_s // 3600)
    minutes = int((uptime_s % 3600) // 60)
    seconds = int(uptime_s % 60)

    # Loop health
    loop_lines = []
    loop_emoji = {0: "🟢", 1: "🟡", 2: "🟠"}
    for name in ("poll", "champagne", "breakout", "eval"):
        backoff = tracker._backoff.get(name, 0)
        emoji = loop_emoji.get(backoff, "🔴")
        delay = tracker._backoff_delay(name)
        if backoff == 0:
            loop_lines.append(f"  {emoji} {name.title()}: healthy")
        else:
            loop_lines.append(
                f"  {emoji} {name.title()}: backoff x{backoff} (+{delay:.0f}s)"
            )

    loop_status = "\n".join(loop_lines)

    return (
        f"📊 <b>Bot Status</b>\n\n"
        f"⏱ <b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
        f"🔄 <b>Shutdown:</b> {'⚠️ Pending' if tracker._shutdown.is_set() else '✅ Running'}\n\n"
        f"<b>Loop Health:</b>\n{loop_status}\n\n"
        f"<b>Session Stats:</b>\n"
        f"  🔍 Tokens discovered: {tracker._stats['tokens_discovered']}\n"
        f"  📝 Tokens scored: {tracker._stats['tokens_scored']}\n"
        f"  🚀 Alerts sent: {tracker._stats['alerts_sent']}\n"
        f"  ❌ Errors: {tracker._stats['errors']}"
    )


async def _build_stats_text(tracker: Tracker) -> str:
    """Build database statistics text."""
    async with tracker._session_factory() as session:
        # Total tokens
        total = await session.scalar(select(func.count(Token.id)))

        # By discovery source
        bankr_ct = await session.scalar(
            select(func.count(Token.id)).where(Token.is_bankr_launch.is_(True))
        )
        champagne_ct = await session.scalar(
            select(func.count(Token.id)).where(Token.is_champagne.is_(True))
        )
        breakout_ct = await session.scalar(
            select(func.count(Token.id)).where(Token.is_breakout.is_(True))
        )

        # Scoring stats
        scored_ct = await session.scalar(
            select(func.count(Token.id)).where(Token.quality_score.isnot(None))
        )
        alerted_ct = await session.scalar(
            select(func.count(Token.id)).where(Token.alert_sent.is_(True))
        )
        unscored_ct = await session.scalar(
            select(func.count(Token.id)).where(Token.quality_score.is_(None))
        )

        # Average score of scored tokens
        avg_score = await session.scalar(
            select(func.avg(Token.quality_score)).where(
                Token.quality_score.isnot(None),
                Token.quality_score > 0,
            )
        )

        # Tokens in last 24h
        cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_ct = await session.scalar(
            select(func.count(Token.id)).where(Token.discovered_at >= cutoff_24h)
        )

    avg_str = f"{avg_score:.1%}" if avg_score else "N/A"
    non_bankr = (total or 0) - (bankr_ct or 0)

    return (
        f"📈 <b>Database Statistics</b>\n\n"
        f"<b>Total Tokens:</b> {total or 0:,}\n"
        f"  🏦 Bankr: {bankr_ct or 0:,}\n"
        f"  💡 Non-Bankr: {non_bankr:,}\n"
        f"  🍾 Champagne: {champagne_ct or 0:,}\n"
        f"  📈 Breakout: {breakout_ct or 0:,}\n\n"
        f"<b>Scoring:</b>\n"
        f"  ✅ Scored: {scored_ct or 0:,}\n"
        f"  ⏳ Pending: {unscored_ct or 0:,}\n"
        f"  📊 Avg score: {avg_str}\n"
        f"  🚀 Alerts sent: {alerted_ct or 0:,}\n\n"
        f"<b>Last 24h:</b> {recent_ct or 0:,} tokens discovered"
    )


async def _build_gems_text(tracker: Tracker) -> str:
    """Build recent gems text (tokens above score threshold)."""
    threshold = tracker.cfg.filtering.score_threshold

    async with tracker._session_factory() as session:
        stmt = (
            select(Token)
            .where(
                Token.quality_score.isnot(None),
                Token.quality_score >= threshold,
            )
            .order_by(Token.discovered_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        gems = list(result.scalars().all())

    if not gems:
        return (
            "💎 <b>Recent Gems</b>\n\n"
            f"No tokens above {threshold:.0%} threshold yet.\n"
            "The scanner is running — gems will appear here when found."
        )

    lines = ["💎 <b>Recent Gems</b>\n"]
    for i, t in enumerate(gems, 1):
        badges = []
        if t.is_champagne:
            badges.append("🍾")
        if t.is_breakout:
            badges.append("📈")
        badge_str = " ".join(badges)
        score_bar = "█" * round((t.quality_score or 0) * 10) + "░" * (10 - round((t.quality_score or 0) * 10))

        addr_short = t.contract_address[:6] + "…" + t.contract_address[-4:]
        lines.append(
            f"\n{i}. {badge_str} <b>{_esc(t.name or '?')}</b> (${_esc(t.symbol or '?')})\n"
            f"   Score: {score_bar} {t.quality_score:.0%}\n"
            f"   <code>{t.contract_address}</code>\n"
            f'   <a href="https://dexscreener.com/base/{t.contract_address}">DexScreener</a>'
            f' | <a href="https://app.uniswap.org/swap?chain=base&outputCurrency={t.contract_address}">Uniswap</a>'
        )

    return "\n".join(lines)


async def _build_top_text(tracker: Tracker) -> str:
    """Build top scored tokens text."""
    async with tracker._session_factory() as session:
        stmt = (
            select(Token)
            .where(
                Token.quality_score.isnot(None),
                Token.quality_score > 0,
            )
            .order_by(Token.quality_score.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        tokens = list(result.scalars().all())

    if not tokens:
        return (
            "🏆 <b>Top Tokens</b>\n\n"
            "No scored tokens yet. Wait for the eval pipeline to process."
        )

    lines = ["🏆 <b>Top Scored Tokens</b>\n"]
    for i, t in enumerate(tokens, 1):
        badges = []
        if t.is_champagne:
            badges.append("🍾")
        if t.is_breakout:
            badges.append("📈")
        badge_str = " ".join(badges)
        score_bar = "█" * round((t.quality_score or 0) * 10) + "░" * (10 - round((t.quality_score or 0) * 10))

        lines.append(
            f"\n{i}. {badge_str} <b>{_esc(t.name or '?')}</b> (${_esc(t.symbol or '?')})\n"
            f"   Score: {score_bar} {t.quality_score:.0%}\n"
            f"   Source: {t.discovery_source or 'firehose'}\n"
            f"   <code>{t.contract_address}</code>"
        )

    return "\n".join(lines)


async def _build_champagne_text(tracker: Tracker) -> str:
    """Build champagne-tagged tokens listing."""
    async with tracker._session_factory() as session:
        stmt = (
            select(Token)
            .where(Token.is_champagne.is_(True))
            .order_by(Token.discovered_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        tokens = list(result.scalars().all())

    if not tokens:
        return (
            "🍾 <b>Champagne Tokens</b>\n\n"
            "No champagne tokens discovered yet.\n"
            "Only ~0.02% of tokens get this curated tag."
        )

    lines = ["🍾 <b>Champagne Tokens</b> (curated by Clanker)\n"]
    for i, t in enumerate(tokens, 1):
        score_str = f"{t.quality_score:.0%}" if t.quality_score is not None else "pending"
        lines.append(
            f"\n{i}. <b>{_esc(t.name or '?')}</b> (${_esc(t.symbol or '?')})\n"
            f"   Score: {score_str} | Alert: {'✅' if t.alert_sent else '⏳'}\n"
            f"   <code>{t.contract_address}</code>\n"
            f'   <a href="https://dexscreener.com/base/{t.contract_address}">DexScreener</a>'
        )

    return "\n".join(lines)


async def _build_breakouts_text(tracker: Tracker) -> str:
    """Build breakout-detected tokens listing."""
    async with tracker._session_factory() as session:
        stmt = (
            select(Token)
            .where(Token.is_breakout.is_(True))
            .order_by(Token.discovered_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        tokens = list(result.scalars().all())

    if not tokens:
        return (
            "📈 <b>Breakout Tokens</b>\n\n"
            "No breakout tokens detected yet.\n"
            "These are delayed movers found via DexScreener trending."
        )

    lines = ["📈 <b>Breakout Tokens</b> (DexScreener trending)\n"]
    for i, t in enumerate(tokens, 1):
        score_str = f"{t.quality_score:.0%}" if t.quality_score is not None else "pending"
        source = (t.discovery_source or "breakout").replace("breakout_", "")
        lines.append(
            f"\n{i}. <b>{_esc(t.name or '?')}</b> (${_esc(t.symbol or '?')})\n"
            f"   Score: {score_str} | Source: {source}\n"
            f"   <code>{t.contract_address}</code>\n"
            f'   <a href="https://dexscreener.com/base/{t.contract_address}">DexScreener</a>'
        )

    return "\n".join(lines)


def _build_config_text(tracker: Tracker) -> str:
    """Build current configuration view."""
    f = tracker.cfg.filtering
    b = tracker.cfg.breakout
    c = tracker.cfg.clanker

    return (
        "⚙️ <b>Current Configuration</b>\n\n"
        "<b>Scan Intervals:</b>\n"
        f"  🔥 Firehose: {c.poll_interval_seconds}s\n"
        f"  🍾 Champagne: {c.champagne_poll_interval_seconds}s\n"
        f"  📈 Breakout: {b.poll_interval_seconds}s\n"
        f"  ⚡ Eval recheck: {f.recheck_delay_seconds}s\n\n"
        "<b>Pre-filter:</b>\n"
        f"  Skip Bankr: {'✅ Yes' if f.skip_bankr else '❌ No'}\n"
        f"  Require socials: {'✅ Yes' if f.require_social_links else '❌ No'}\n"
        f"  Champagne auto-pass: {'✅ Yes' if f.champagne_auto_pass_stage1 else '❌ No'}\n\n"
        "<b>Scoring Thresholds:</b>\n"
        f"  Alert threshold: {f.score_threshold:.0%}\n"
        f"  Min liquidity: ${f.min_pool_liquidity_usd:,.0f}\n"
        f"  Min volume 1h: ${f.min_volume_1h_usd:,.0f}\n"
        f"  Min mcap: ${f.min_mcap_usd:,.0f}\n"
        f"  Min holders: {f.min_holders}\n\n"
        "<b>Breakout Scanner:</b>\n"
        f"  Enabled: {'✅ Yes' if b.enabled else '❌ No'}\n"
        f"  Min liquidity: ${b.min_liquidity_usd:,.0f}\n"
        f"  Min volume 24h: ${b.min_volume_24h_usd:,.0f}\n"
        f"  Score bonus: +{b.breakout_score_bonus:.0%}\n\n"
        "<b>Scoring Weights:</b>\n"
        f"  Metrics: {f.weight_metrics}\n"
        f"  Momentum: {f.weight_momentum}\n"
        f"  Smart money: {f.weight_smart_money}\n"
        f"  Context: {f.weight_context}\n"
        f"  Champagne bonus: +{f.weight_champagne_bonus:.0%}"
    )


def _build_help_text() -> str:
    """Build help text with all available commands."""
    return (
        "❓ <b>Base Sleuth Commands</b>\n\n"
        "<b>Dashboard:</b>\n"
        "/start, /menu — Main dashboard with buttons\n\n"
        "<b>Monitoring:</b>\n"
        "/status — Bot uptime, loop health, error counts\n"
        "/stats — Database stats (tokens, scoring, alerts)\n\n"
        "<b>Discovery:</b>\n"
        "/gems — Recent gems above score threshold\n"
        "/top — Top 10 highest scored tokens\n"
        "/scan — Force scan (firehose, champagne, breakout)\n\n"
        "<b>Browse:</b>\n"
        "🍾 Champagne — View champagne-tagged tokens\n"
        "📈 Breakouts — View breakout-detected tokens\n\n"
        "<b>Settings:</b>\n"
        "/config — View current filter configuration\n"
        "/help — This help message\n\n"
        "<i>All buttons work from the main menu too!</i>"
    )


# ──────────────────────────────────────────────────────────────────
# Force scan handler
# ──────────────────────────────────────────────────────────────────

async def _handle_force_scan(query, tracker: Tracker, scan_type: str) -> None:
    """Trigger a force scan and report results."""
    scan_labels = {
        "firehose": "🔥 Firehose",
        "champagne": "🍾 Champagne",
        "breakout": "📈 Breakout",
        "eval": "⚡ Eval Pipeline",
        "all": "🔄 All Scanners",
    }
    label = scan_labels.get(scan_type, scan_type)

    await query.edit_message_text(
        f"⏳ <b>Running {label}...</b>\n\nPlease wait.",
        parse_mode=ParseMode.HTML,
    )

    try:
        results = []

        if scan_type in ("firehose", "all"):
            async with tracker._session_factory() as session:
                tokens = await tracker._client.poll(session)
                await session.commit()
                count = len(tokens) if tokens else 0
                results.append(f"🔥 Firehose: {count} tokens")

        if scan_type in ("champagne", "all"):
            async with tracker._session_factory() as session:
                tokens = await tracker._client.poll_champagne(session)
                await session.commit()
                count = len(tokens) if tokens else 0
                results.append(f"🍾 Champagne: {count} tokens")

        if scan_type in ("breakout", "all"):
            if tracker.cfg.breakout.enabled and tracker._breakout:
                async with tracker._session_factory() as session:
                    tokens = await tracker._breakout.scan(session)
                    await session.commit()
                    count = len(tokens) if tokens else 0
                    results.append(f"📈 Breakout: {count} tokens")
            else:
                results.append("📈 Breakout: disabled")

        if scan_type in ("eval", "all"):
            results.append("⚡ Eval: triggered (runs in background)")
            # Reset eval backoff to trigger immediate processing
            tracker._backoff["eval"] = 0

        result_text = "\n".join(results)
        await query.edit_message_text(
            f"✅ <b>{label} Complete</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML,
            reply_markup=_scan_menu_keyboard(),
        )
        logger.info("force_scan.complete", scan_type=scan_type, results=results)

    except Exception as exc:
        logger.exception("force_scan.error", scan_type=scan_type)
        await query.edit_message_text(
            f"❌ <b>Scan Failed</b>\n\n<code>{_esc(str(exc)[:200])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_scan_menu_keyboard(),
        )


# ──────────────────────────────────────────────────────────────────
# Application builder
# ──────────────────────────────────────────────────────────────────

def build_telegram_app(bot_token: str, tracker: Tracker) -> Application:
    """Build a python-telegram-bot Application with all handlers.

    The Application is configured for non-blocking integration
    alongside the existing scanner async loops.
    """
    app = Application.builder().token(bot_token).build()

    # Store tracker reference for handlers to access
    app.bot_data["tracker"] = tracker

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("gems", cmd_gems))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("help", cmd_help))

    # Register callback query handler for all inline buttons
    app.add_handler(CallbackQueryHandler(button_callback))

    return app


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
