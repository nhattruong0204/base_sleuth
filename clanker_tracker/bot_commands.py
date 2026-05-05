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
    MessageHandler,
    filters,
)
from sqlalchemy import func, select

from .models import AlertOutcome, PaperPosition, SmartWallet, Token, TokenContext, TokenMetrics, WalletSwap
from .nansen_listener import NansenIngestor, parse_nansen_message

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
            InlineKeyboardButton("💰 PnL Report", callback_data="pnl_1"),
            InlineKeyboardButton("🔬 Analysis", callback_data="analysis"),
        ],
        [
            InlineKeyboardButton("👛 Wallets", callback_data="wallets"),
            InlineKeyboardButton("📝 Positions", callback_data="positions"),
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

def _pnl_keyboard() -> InlineKeyboardMarkup:
    """PnL timeframe switcher + back button."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1D", callback_data="pnl_1"),
            InlineKeyboardButton("7D", callback_data="pnl_7"),
            InlineKeyboardButton("14D", callback_data="pnl_14"),
        ],
        [InlineKeyboardButton("\u2b05\ufe0f Back to Menu", callback_data="menu")],
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


async def cmd_realpnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /realpnl <days> — real-time PnL report for alerted tokens."""
    tracker: Tracker = context.bot_data["tracker"]

    # Parse days argument (default 1)
    days = 1
    if context.args:
        try:
            days = int(context.args[0])
            if days < 1:
                days = 1
            elif days > 30:
                days = 30
        except ValueError:
            await update.message.reply_text(
                "\u274c Usage: /realpnl <days>\nExample: /realpnl 7",
                parse_mode=ParseMode.HTML,
            )
            return

    await update.message.reply_text(
        f"\u23f3 Fetching live prices for alerts in last {days} day(s)...\nThis may take a moment.",
        parse_mode=ParseMode.HTML,
    )

    text = await _build_realpnl_text(tracker, days)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_pnl_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /wallets — smart wallet tracking summary."""
    tracker: Tracker = context.bot_data["tracker"]
    text = await _build_wallets_text(tracker)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=_back_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /analysis — full token profitability scan with live DexScreener."""
    tracker: Tracker = context.bot_data["tracker"]
    await update.message.reply_text(
        "⏳ Running full profitability analysis...\nFetching live prices for all scored tokens. This may take 30-60s.",
        parse_mode=ParseMode.HTML,
    )
    text = await _build_analysis_text(tracker)
    # Telegram has a 4096-char limit; split if needed
    for chunk in _split_message(text):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /positions — paper trading portfolio dashboard."""
    tracker: Tracker = context.bot_data["tracker"]
    text = await _build_positions_text(tracker)
    for chunk in _split_message(text):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
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

    elif data.startswith("pnl_"):
        days = int(data.split("_")[1])
        await query.edit_message_text(
            f"\u23f3 Fetching live prices for alerts in last {days} day(s)...",
            parse_mode=ParseMode.HTML,
        )
        text = await _build_realpnl_text(tracker, days)
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_pnl_keyboard(),
            disable_web_page_preview=True,
        )

    elif data == "wallets":
        text = await _build_wallets_text(tracker)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )

    elif data == "analysis":
        await query.edit_message_text(
            "⏳ Running full profitability analysis...\nFetching live prices for all scored tokens.",
            parse_mode=ParseMode.HTML,
        )
        text = await _build_analysis_text(tracker)
        chunks = _split_message(text)
        # First chunk replaces the loading message
        await query.edit_message_text(
            chunks[0],
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )
        # Additional chunks sent as new messages
        for chunk in chunks[1:]:
            await query.message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=_back_keyboard(),
                disable_web_page_preview=True,
            )

    elif data == "positions":
        text = await _build_positions_text(tracker)
        chunks = _split_message(text)
        await query.edit_message_text(
            chunks[0],
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
            disable_web_page_preview=True,
        )
        for chunk in chunks[1:]:
            await query.message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=_back_keyboard(),
                disable_web_page_preview=True,
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

    # Display names for all 16 loops
    loop_names = {
        "poll": "Firehose",
        "champagne": "Champagne",
        "breakout": "Breakout",
        "gainers": "Gainers",
        "binance_trending": "Binance Trending",
        "eval": "Eval Pipeline",
        "outcome": "PID Outcome",
        "wallet_sync": "Wallet Sync",
        "wallet_monitor": "Wallet Monitor",
        "wallet_watch": "Wallet Watch",
        "nansen": "Nansen Listener",
        "champagne_eval": "Champagne Eval",
        "paper_trading": "Paper Trading",
        "multi_conviction": "Multi Conviction",
        "token_flow": "Token Flow",
        "portfolio_watch": "Portfolio Watch",
        "milestone_tracker": "Milestone Tracker",
        "gate_pending": "Gate Pending",
    }

    for key, display in loop_names.items():
        backoff = tracker._backoff.get(key, 0)
        emoji = loop_emoji.get(backoff, "🔴")
        delay = tracker._backoff_delay(key)
        if backoff == 0:
            loop_lines.append(f"  {emoji} {display}: healthy")
        else:
            loop_lines.append(
                f"  {emoji} {display}: backoff x{backoff} (+{delay:.0f}s)"
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
    m = tracker.cfg.minara
    minara_ready = (
        m.enabled
        and (
            (m.auth_method == "api_key" and m.api_key)
            or (m.auth_method == "x402" and m.x402_evm_private_key)
        )
    )

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
        f"  Champagne bonus: +{f.weight_champagne_bonus:.0%}\n\n"
        "<b>Minara AI:</b>\n"
        f"  Enabled: {'✅ Yes' if minara_ready else '❌ No'}\n"
        f"  Auth: {m.auth_method}\n"
        f"  Mode: {m.mode}\n"
        f"  Append thesis: {'✅ Yes' if m.include_thesis else '❌ No'}\n"
        f"  Gate alerts: {'✅ Yes' if m.gate_alerts else '❌ No'}"
    )


async def _build_wallets_text(tracker: Tracker) -> str:
    """Build smart wallet tracking summary."""
    if not tracker._wallet_tracker or not tracker._arkham or not tracker._arkham.enabled:
        return (
            "👛 <b>Smart Wallet Tracking</b>\n\n"
            "⚠️ Arkham Intel integration is <b>disabled</b>.\n\n"
            "To enable:\n"
            "1. Get an API key from intel.arkm.com\n"
            "2. Set <code>arkham.api_key</code> in config.yaml\n"
            "3. Set <code>arkham.enabled: true</code>\n"
            "4. Restart the bot"
        )

    stats = await tracker._wallet_tracker.get_wallet_stats()

    lines = [
        "👛 <b>Smart Wallet Tracking</b>",
        "",
        f"Tag: <code>{tracker.cfg.arkham.tag_id}</code>",
        f"Total wallets: <b>{stats['total']}</b>",
        f"Active (profitable): <b>{stats['active']}</b>",
        "",
        f"🥇 Tier 1: {stats['tier1']}  |  🥈 Tier 2: {stats['tier2']}  |  🥉 Tier 3: {stats['tier3']}",
        "",
        f"📊 Swaps (24h): {stats['swaps_24h']}",
        f"🔥 Convictions (24h): {stats['convictions_24h']}",
        "",
    ]

    top = stats.get("top_wallets", [])
    if top:
        lines.append("<b>Top Tracked Wallets:</b>")
        for w in top:
            tier_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(w.tier, "🔘")
            addr_short = w.address[:8] + "…" + w.address[-4:]
            pnl_parts = []
            if w.pnl_7d_pct is not None:
                pnl_parts.append(f"7d: {w.pnl_7d_pct:+.1f}%")
            if w.pnl_30d_pct is not None:
                pnl_parts.append(f"30d: {w.pnl_30d_pct:+.1f}%")
            pnl_str = " | ".join(pnl_parts) if pnl_parts else "N/A"
            label = f" ({_esc(w.arkham_label)})" if w.arkham_label else ""
            lines.append(
                f"  {tier_emoji} <code>{addr_short}</code>{label} — {pnl_str}"
            )

    return "\n".join(lines)


async def _build_positions_text(tracker: Tracker) -> str:
    """Build paper trading portfolio dashboard."""
    if not tracker.cfg.paper_trading.enabled:
        return (
            "📝 <b>Paper Trading</b>\n\n"
            "⚠️ Paper trading is <b>disabled</b>.\n\n"
            "To enable, set <code>paper_trading.enabled: true</code> in config.yaml"
        )

    async with tracker._session_factory() as session:
        # Open positions
        open_stmt = (
            select(PaperPosition)
            .where(PaperPosition.status == "open")
            .order_by(PaperPosition.opened_at.desc())
            .limit(15)
        )
        open_result = await session.execute(open_stmt)
        open_positions = open_result.scalars().all()

        # Count open
        open_count_r = await session.execute(
            select(func.count()).select_from(PaperPosition).where(PaperPosition.status == "open")
        )
        open_count = open_count_r.scalar() or 0

        # Closed positions stats
        closed_stmt = (
            select(
                func.count().label("total"),
                func.sum(PaperPosition.realized_pnl_usd).label("total_pnl"),
                func.avg(PaperPosition.realized_pnl_usd).label("avg_pnl"),
            )
            .select_from(PaperPosition)
            .where(PaperPosition.status != "open")
        )
        closed_r = await session.execute(closed_stmt)
        closed = closed_r.one()
        closed_total = closed.total or 0
        total_pnl = closed.total_pnl or 0.0
        avg_pnl = closed.avg_pnl or 0.0

        # Win rate (closed with positive PnL)
        wins_r = await session.execute(
            select(func.count()).select_from(PaperPosition)
            .where(PaperPosition.status != "open")
            .where(PaperPosition.realized_pnl_usd > 0)
        )
        wins = wins_r.scalar() or 0
        win_rate = (wins / closed_total * 100) if closed_total > 0 else 0.0

        # Status breakdown
        status_r = await session.execute(
            select(PaperPosition.status, func.count())
            .group_by(PaperPosition.status)
        )
        status_counts = {row[0]: row[1] for row in status_r.all()}

    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    lines = [
        "📝 <b>Paper Trading Dashboard</b>",
        "",
        f"📊 <b>Portfolio Summary</b>",
        f"  Open: <b>{open_count}</b>  |  Closed: <b>{closed_total}</b>",
        f"  {pnl_emoji} Total PnL: <b>${total_pnl:+,.2f}</b>",
        f"  Avg PnL/trade: <b>${avg_pnl:+,.2f}</b>",
        f"  Win rate: <b>{win_rate:.1f}%</b> ({wins}/{closed_total})",
        "",
        f"📋 <b>Exit Breakdown:</b>",
        f"  🎯 TP1 (+50%): {status_counts.get('tp1', 0)}",
        f"  🎯 TP2 (+100%): {status_counts.get('tp2', 0)}",
        f"  🎯 TP3 (+300%): {status_counts.get('tp3', 0)}",
        f"  🛑 SL (-30%): {status_counts.get('sl', 0)}",
        f"  ⏰ Time stop: {status_counts.get('time_stop', 0)}",
        "",
    ]

    if open_positions:
        lines.append("<b>📈 Open Positions (latest 15):</b>")
        for pos in open_positions:
            token = pos.token
            name = _esc(token.name or token.symbol or "???")[:20]
            entry_p = pos.entry_price_usd or 0
            current_p = pos.current_price_usd or entry_p
            pnl_pct = ((current_p - entry_p) / entry_p * 100) if entry_p > 0 else 0
            arrow = "🟢" if pnl_pct >= 0 else "🔴"
            mcap_str = f"${pos.last_check_mcap / 1000:.0f}K" if pos.last_check_mcap and pos.last_check_mcap < 1_000_000 else f"${pos.last_check_mcap / 1_000_000:.1f}M" if pos.last_check_mcap else "?"
            age_h = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600 if pos.opened_at else 0
            lines.append(
                f"  {arrow} <b>{name}</b> — {pnl_pct:+.1f}% | MCap {mcap_str} | {age_h:.1f}h"
            )
    else:
        lines.append("<i>No open positions</i>")

    return "\n".join(lines)


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
        "<b>Analytics:</b>\n"
        "/realpnl <i>days</i> — Real-time PnL report (1/7/14 days)\n"
        "/analysis — Full token profitability scan (live prices)\n"
        "/wallets — Smart wallet tracking summary\n"
        "/positions — Paper trading portfolio dashboard\n\n"
        "<b>Browse:</b>\n"
        "🍾 Champagne — View champagne-tagged tokens\n"
        "📈 Breakouts — View breakout-detected tokens\n\n"
        "<b>Settings:</b>\n"
        "/config — View current filter configuration\n"
        "/help — This help message\n\n"
        "<i>All buttons work from the main menu too!</i>"
    )


# ──────────────────────────────────────────────────────────────────
# Real-Time PnL builder
# ──────────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    """Compact number formatter for PnL display."""
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    return f"{val:,.0f}"


async def _build_realpnl_text(tracker: Tracker, days: int) -> str:
    """Build real-time PnL report by fetching live DexScreener data.

    For each alerted token in the timeframe:
    - Looks up alert-time FDV from AlertOutcome (or TokenMetrics fallback)
    - Fetches live FDV from DexScreener
    - Computes return multiplier = live_fdv / alert_fdv
    - Simulates 1 SOL per trade with GMGN fee model (2.5% buy + 2.5% sell)
    """
    import httpx

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    date_from = cutoff.strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    # Step 1: Get all alerted tokens in the timeframe
    async with tracker._session_factory() as session:
        stmt = (
            select(Token)
            .where(Token.alert_sent.is_(True))
            .where(Token.discovered_at >= cutoff)
            .order_by(Token.discovered_at.desc())
        )
        result = await session.execute(stmt)
        tokens = list(result.scalars().all())

        if not tokens:
            return (
                f"📊 <b>Real-Time PnL</b> — Last {days} Day(s)\n\n"
                f"📅 {date_from} to {date_to}\n\n"
                "No alerts were sent in this timeframe."
            )

        # Step 2: Get alert-time FDV for each token from AlertOutcome
        token_ids = [t.id for t in tokens]
        ao_stmt = (
            select(AlertOutcome)
            .where(AlertOutcome.token_id.in_(token_ids))
        )
        ao_result = await session.execute(ao_stmt)
        outcomes: dict[int, AlertOutcome] = {
            ao.token_id: ao for ao in ao_result.scalars().all()
        }

        # Fallback: get latest TokenMetrics for tokens without AlertOutcome
        for tok in tokens:
            if tok.id not in outcomes:
                m_stmt = (
                    select(TokenMetrics)
                    .where(TokenMetrics.token_id == tok.id)
                    .order_by(TokenMetrics.snapshot_at.desc())
                    .limit(1)
                )
                m_row = (await session.execute(m_stmt)).scalar_one_or_none()
                if m_row:
                    # Create a pseudo-outcome for uniform handling
                    outcomes[tok.id] = AlertOutcome(
                        token_id=tok.id,
                        alert_fdv=m_row.fdv_usd,
                        alert_mcap=m_row.market_cap_usd,
                        alert_liq=m_row.liquidity_usd,
                    )

    # Step 3: Batch-fetch live DexScreener data
    live_metrics: dict[str, dict] = {}
    dex_url = tracker.cfg.dexscreener.base_url
    batch_size = tracker.cfg.dexscreener.batch_size

    for i in range(0, len(tokens), batch_size):
        batch = tokens[i: i + batch_size]
        addrs = ",".join(t.contract_address for t in batch if t.contract_address)
        if not addrs:
            continue
        try:
            resp = await tracker._http.get(
                f"{dex_url}/tokens/{addrs}", timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            seen: set[str] = set()
            for pair in data.get("pairs") or []:
                addr = pair.get("baseToken", {}).get("address", "").lower()
                if addr and addr not in seen:
                    seen.add(addr)
                    live_metrics[addr] = {
                        "fdv": float(pair.get("fdv") or 0),
                        "mcap": float(pair.get("marketCap") or 0),
                        "liq": float((pair.get("liquidity") or {}).get("usd", 0)),
                    }
        except Exception as exc:
            logger.warning("realpnl.dex_fetch_error", error=str(exc))

    # Step 4: Compute PnL for each token
    SOL_PER_TRADE = 1.0
    BUY_FEE_PCT = 2.5 / 100   # GMGN buy fee
    SELL_FEE_PCT = 2.5 / 100  # GMGN sell fee

    total_signals = len(tokens)
    priced_ok = 0
    rugged = 0
    winners = 0
    losers = 0
    total_invested = 0.0
    total_returned = 0.0
    total_buy_fees = 0.0
    total_sell_fees = 0.0

    token_details: list[dict] = []

    for tok in tokens:
        ao = outcomes.get(tok.id)
        alert_fdv = (ao.alert_fdv or 0) if ao else 0
        addr = tok.contract_address.lower() if tok.contract_address else ""
        live = live_metrics.get(addr)

        if not live or live["fdv"] == 0:
            # Token is rugged / dead — zero return
            rugged += 1
            total_invested += SOL_PER_TRADE
            buy_fee = SOL_PER_TRADE * BUY_FEE_PCT
            total_buy_fees += buy_fee
            token_details.append({
                "symbol": tok.symbol or "???",
                "multiplier": 0.0,
                "returned": 0.0,
                "rugged": True,
            })
            continue

        priced_ok += 1
        live_fdv = live["fdv"]

        # Multiplier: how much did FDV change
        if alert_fdv > 0:
            multiplier = live_fdv / alert_fdv
        else:
            multiplier = 1.0  # No alert FDV → assume break-even

        # Simulate trade: invest 1 SOL, subtract buy fee
        invested = SOL_PER_TRADE
        buy_fee = invested * BUY_FEE_PCT
        position_value = (invested - buy_fee) * multiplier

        # Sell: subtract sell fee
        sell_fee = position_value * SELL_FEE_PCT
        returned = position_value - sell_fee

        total_invested += invested
        total_returned += returned
        total_buy_fees += buy_fee
        total_sell_fees += sell_fee

        if multiplier >= 1.0:
            winners += 1
        else:
            losers += 1

        token_details.append({
            "symbol": tok.symbol or "???",
            "multiplier": multiplier,
            "returned": returned,
            "rugged": False,
        })

    # Step 5: Build the message
    net_profit = total_returned - total_invested
    net_pct = (net_profit / total_invested * 100) if total_invested > 0 else 0
    total_fees = total_buy_fees + total_sell_fees
    fee_pct = (total_fees / total_invested * 100) if total_invested > 0 else 0
    gross_profit = net_profit + total_fees
    gross_pct = (gross_profit / total_invested * 100) if total_invested > 0 else 0

    win_rate = (winners / (priced_ok + rugged) * 100) if (priced_ok + rugged) > 0 else 0
    pnl_emoji = "🟢" if net_profit >= 0 else "🔴"

    lines = [
        f"📊 <b>Real-Time PnL</b> — Last {days} Day(s)\n",
        f"📅 {date_from} to {date_to}\n",
        f"💰 <b>SOL Performance ({SOL_PER_TRADE} SOL/trade)</b>",
        f"• Total Invested: {total_invested:.2f} SOL",
        f"• Total Returned: {total_returned:.2f} SOL",
        f"• {pnl_emoji} Net Profit: {net_profit:+.2f} SOL ({net_pct:+.1f}%)\n",
        "💸 <b>Fee Breakdown (GMGN)</b>",
        f"• Buy Fees (2.5%): {total_buy_fees:.2f} SOL",
        f"• Sell Fees (2.5%): {total_sell_fees:.2f} SOL",
        f"• Total Fees: {total_fees:.2f} SOL ({fee_pct:.1f}% of invested)",
        f"• Gross Profit: {gross_profit:+.2f} SOL ({gross_pct:+.1f}%)\n",
        "📈 <b>Overview</b>",
        f"• Total Signals: {total_signals}",
        f"• Priced OK: {priced_ok}",
        f"• Rugged: {rugged} 💀\n",
        "📊 <b>Win/Loss</b>",
        f"• {pnl_emoji} Win Rate: {win_rate:.1f}%",
        f"• Winners (≥1X): {winners}",
        f"• Losers (&lt;1X): {losers}",
    ]

    # Top 5 winners
    sorted_details = sorted(
        [d for d in token_details if not d["rugged"]],
        key=lambda d: d["multiplier"],
        reverse=True,
    )
    top_winners = [d for d in sorted_details if d["multiplier"] >= 1.0][:5]
    if top_winners:
        lines.append("\n🏆 <b>Top Winners</b>")
        for d in top_winners:
            lines.append(
                f"• ${_esc(d['symbol'])}: {d['multiplier']:.2f}X "
                f"→ {d['returned']:.2f} SOL"
            )

    # Worst 5 losers (non-rugged)
    worst_losers = [d for d in sorted_details if d["multiplier"] < 1.0][-5:]
    if worst_losers:
        lines.append("\n💀 <b>Worst Losers</b>")
        for d in reversed(worst_losers):
            lines.append(
                f"• ${_esc(d['symbol'])}: {d['multiplier']:.2f}X "
                f"→ {d['returned']:.2f} SOL"
            )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# Full profitability analysis builder
# ──────────────────────────────────────────────────────────────────

async def _build_analysis_text(tracker: Tracker) -> str:
    """Full profitability analysis: fetch live DexScreener prices for ALL
    tokens with recorded FDV, compare, and produce a rich summary."""
    import json as _json

    now = datetime.now(timezone.utc)

    # Step 1: pull all tokens that have at least one FDV metric snapshot
    async with tracker._session_factory() as session:
        total_tokens_row = await session.scalar(select(func.count(Token.id)))
        total_tokens = total_tokens_row or 0

        # One row per token: earliest metric snapshot (= first score time)
        stmt = (
            select(
                Token.id,
                Token.symbol,
                Token.name,
                Token.contract_address,
                Token.quality_score,
                Token.alert_sent,
                Token.is_champagne,
                Token.is_breakout,
                Token.discovery_source,
                TokenMetrics.fdv_usd,
                TokenMetrics.market_cap_usd,
                TokenMetrics.liquidity_usd,
            )
            .join(TokenMetrics, TokenMetrics.token_id == Token.id)
            .where(TokenMetrics.fdv_usd.isnot(None), TokenMetrics.fdv_usd > 0)
            .order_by(Token.id, TokenMetrics.snapshot_at.asc())
            .distinct(Token.id)
        )
        rows = (await session.execute(stmt)).all()

    if not rows:
        return (
            "🔬 <b>Full Profitability Analysis</b>\n\n"
            "No tokens with recorded FDV found in the database yet."
        )

    # Build lookup
    token_data = []
    for r in rows:
        token_data.append({
            "id": r[0], "symbol": r[1] or "???", "name": r[2] or "",
            "address": r[3], "score": r[4] or 0,
            "alerted": r[5], "champagne": r[6], "breakout": r[7],
            "source": r[8] or "firehose",
            "rec_fdv": r[9] or 0, "rec_mcap": r[10] or 0, "rec_liq": r[11] or 0,
        })

    # Step 2: batch-fetch live DexScreener data
    dex_url = tracker.cfg.dexscreener.base_url
    batch_size = tracker.cfg.dexscreener.batch_size
    live: dict[str, dict] = {}

    for i in range(0, len(token_data), batch_size):
        batch = token_data[i : i + batch_size]
        addrs = ",".join(t["address"] for t in batch if t["address"])
        if not addrs:
            continue
        try:
            resp = await tracker._http.get(f"{dex_url}/tokens/{addrs}", timeout=15)
            resp.raise_for_status()
            data = resp.json()
            seen: set[str] = set()
            for pair in data.get("pairs") or []:
                addr = pair.get("baseToken", {}).get("address", "").lower()
                if addr and addr not in seen:
                    seen.add(addr)
                    live[addr] = {
                        "fdv": float(pair.get("fdv") or 0),
                        "mcap": float(pair.get("marketCap") or 0),
                        "liq": float((pair.get("liquidity") or {}).get("usd", 0)),
                    }
        except Exception as exc:
            logger.warning("analysis.dex_error", error=str(exc))

    # Step 3: classify
    profitable = []
    losers_list = []
    dead = []

    for t in token_data:
        addr = t["address"].lower()
        lv = live.get(addr)
        if not lv or lv["fdv"] == 0:
            dead.append(t)
            continue
        rec = t["rec_fdv"]
        if rec <= 0:
            continue
        mult = lv["fdv"] / rec
        entry = {**t, "live_fdv": lv["fdv"], "live_mcap": lv["mcap"],
                 "live_liq": lv["liq"], "mult": mult, "pnl": (mult - 1) * 100}
        if mult >= 1.0:
            profitable.append(entry)
        else:
            losers_list.append(entry)

    profitable.sort(key=lambda x: x["mult"], reverse=True)
    losers_list.sort(key=lambda x: x["mult"])

    n_total = len(token_data)
    n_dead = len(dead)
    n_prof = len(profitable)
    n_loss = len(losers_list)
    n_alive = n_prof + n_loss
    big2x = [p for p in profitable if p["mult"] >= 2.0]
    big5x = [p for p in profitable if p["mult"] >= 5.0]

    # Alert breakdown
    a_prof = [p for p in profitable if p["alerted"]]
    a_loss = [l for l in losers_list if l["alerted"]]
    a_dead = [d for d in dead if d["alerted"]]
    n_alerted = len(a_prof) + len(a_loss) + len(a_dead)

    pct = lambda n, d: f"{n/d*100:.1f}%" if d else "0%"

    # Step 4: build message
    lines = [
        "🔬 <b>Full Token Profitability Analysis</b>\n",
        f"📅 Scanned: {now.strftime('%Y-%m-%d %H:%M')} UTC",
        f"Database: {total_tokens:,} total tokens | {n_total:,} with FDV data\n",
        "<b>Overall Numbers</b>",
        f"💀 Dead (zero DexScreener data): {n_dead} ({pct(n_dead, n_total)})",
        f"📈 Profitable (≥1X): {n_prof} ({pct(n_prof, n_total)})",
        f"📉 Losers (&lt;1X): {n_loss} ({pct(n_loss, n_total)})",
        f"<b>Still alive: {n_alive} ({pct(n_alive, n_total)})</b>",
        f"\n🏆 2X+ winners: {len(big2x)}",
        f"🚀 5X+ rockets: {len(big5x)}\n",
    ]

    # Top profitable
    top_n = profitable[:15]
    if top_n:
        lines.append("<b>Top Profitable Tokens</b>")
        for i, t in enumerate(top_n, 1):
            badges = ""
            if t["alerted"]:
                badges += "✅"
            if t["champagne"]:
                badges += "🍾"
            if t["breakout"]:
                badges += "📈"
            lines.append(
                f"{i}. {badges}<b>${_esc(t['symbol'][:15])}</b> "
                f"${_fmt(t['rec_fdv'])}→${_fmt(t['live_fdv'])} "
                f"<b>{t['mult']:.2f}X</b> ({t['pnl']:+.0f}%) "
                f"liq ${_fmt(t['live_liq'])}"
            )
        lines.append("")

    # Alert performance
    lines.append(f"<b>Alert Performance</b> ({n_alerted} total alerts)")
    lines.append(f"✅ Profitable: {len(a_prof)} ({pct(len(a_prof), n_alerted)})")
    lines.append(f"🔴 Losers: {len(a_loss)} ({pct(len(a_loss), n_alerted)})")
    lines.append(f"💀 Dead: {len(a_dead)} ({pct(len(a_dead), n_alerted)})")
    lines.append("")

    # Real winners among alerted
    all_alerted_alive = sorted(a_prof + a_loss, key=lambda x: x["mult"], reverse=True)
    if all_alerted_alive:
        lines.append("<b>Alerted Tokens — Current Status</b>")
        for t in all_alerted_alive[:20]:
            emoji = "🟢" if t["mult"] >= 1 else "🔴"
            badges = ""
            if t["champagne"]:
                badges += "🍾"
            if t["breakout"]:
                badges += "📈"
            lines.append(
                f"{emoji}{badges} <b>${_esc(t['symbol'][:13])}</b> "
                f"${_fmt(t['rec_fdv'])}→${_fmt(t['live_fdv'])} "
                f"{t['mult']:.2f}X ({t['pnl']:+.1f}%)"
            )
        for t in a_dead:
            lines.append(
                f"💀 <b>${_esc(t['symbol'][:13])}</b> "
                f"${_fmt(t['rec_fdv'])}→DEAD"
            )
        lines.append("")

    # Missed gems (2X+ but not alerted)
    missed = [p for p in profitable if p["mult"] >= 1.5 and not p["alerted"]]
    if missed:
        lines.append(f"<b>Missed Gems</b> (≥1.5X but not alerted: {len(missed)})")
        for t in missed[:10]:
            badges = "🍾" if t["champagne"] else ""
            lines.append(
                f"⚠️{badges} <b>${_esc(t['symbol'][:13])}</b> "
                f"score {t['score']:.2f} | {t['mult']:.2f}X "
                f"${_fmt(t['rec_fdv'])}→${_fmt(t['live_fdv'])} "
                f"({t['source']})"
            )
        lines.append("")

    # Key insight
    lines.append("<b>Key Insight</b>")
    if n_alive > 0:
        alive_pct = n_prof / n_alive * 100
        lines.append(
            f"Of {n_alive} alive tokens, {n_prof} ({alive_pct:.0f}%) are at/above recorded FDV. "
        )
    if big2x:
        syms = ", ".join(f"${t['symbol']}" for t in big2x[:5])
        lines.append(f"2X+ winners: {syms}")
    if not big2x:
        lines.append("No tokens hit 2X yet.")

    return "\n".join(lines)


def _split_message(text: str, limit: int = 4000) -> list[str]:
    """Split a long message into chunks that fit Telegram's 4096-char limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find last newline before the limit
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


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
# NansenBot message handler
# ──────────────────────────────────────────────────────────────────

async def handle_nansen_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages — check if they're NansenBot alerts.

    Triggers on:
    1. Messages forwarded from NansenBot (forward_origin check)
    2. Messages containing "Smart Alert" + "nansen.ai" patterns
    3. Messages you manually paste from NansenBot

    When a valid Base chain signal is found:
    - Wallet + label saved to smart_wallets + watchlist
    - Swap recorded in wallet_swaps
    - Conviction check (token in our DB?)
    - Alert forwarded to chat
    """
    tracker: Tracker = context.bot_data["tracker"]
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text

    # Quick filter — skip messages that don't look like Nansen alerts
    if "Smart Alert" not in text and "nansen.ai" not in text:
        return

    nansen_cfg = tracker.cfg.nansen
    if not nansen_cfg.enabled:
        return

    # Parse the Nansen message
    signal = parse_nansen_message(text)
    if not signal:
        return

    # Only process Base chain
    if nansen_cfg.base_only and not signal.is_base:
        return

    logger.info(
        "nansen.message_received",
        wallet=signal.wallet_label or signal.wallet_address[:10],
        token=signal.token_symbol,
        usd=signal.usd_value,
    )

    # Process through pipeline
    try:
        ingestor = NansenIngestor(tracker._session_factory)
        result = await ingestor.ingest(signal)

        # Update filters' smart wallet set if new wallet added
        if result["wallet_new"] and tracker._filter:
            tracker._filter._smart_wallets.add(signal.wallet_address.lower())
            if tracker._wallet_monitor:
                tracker._wallet_monitor._wallet_addresses.add(signal.wallet_address.lower())
                tracker._wallet_monitor._alias_map[signal.wallet_address.lower()] = signal.wallet_label

        # Forward as alert
        if nansen_cfg.forward_alerts and tracker._notifier:
            await tracker._notifier.notify_nansen_signal(signal.to_dict())

        # Send conviction alert if applicable
        if result["conviction"] and tracker._notifier:
            await tracker._notifier.notify_conviction(result["conviction"])

        # Brief confirmation reply (optional, can be removed for stealth)
        status_parts = []
        if result["wallet_new"]:
            status_parts.append(f"👛 New wallet: {signal.wallet_label or signal.wallet_address[:10]}")
        if result["swap_new"]:
            status_parts.append(f"💾 Swap recorded")
        if result["conviction"]:
            status_parts.append(f"🔥 CONVICTION — token in DB!")

        if status_parts:
            await msg.reply_text(
                "✅ Nansen signal processed:\n" + "\n".join(status_parts),
                parse_mode=ParseMode.HTML,
            )

    except Exception:
        logger.exception("nansen.handler_error")


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
    app.add_handler(CommandHandler("realpnl", cmd_realpnl))
    app.add_handler(CommandHandler("analysis", cmd_analysis))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("positions", cmd_positions))

    # Register callback query handler for all inline buttons
    app.add_handler(CallbackQueryHandler(button_callback))

    # NansenBot message listener — catches forwarded messages and
    # any text matching Nansen alert patterns
    if tracker.cfg.nansen.enabled:
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_nansen_message,
        ))
        logger.info("nansen_listener.registered")

    return app


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
