import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)

from src.bot.filters import MediaURLFilter
from src.bot.handlers import (
    handle_chapter_page_callback,
    handle_chapters,
    handle_help,
    handle_redownload,
    handle_refresh,
    handle_start,
    handle_url,
)
from src.cache import CompositeCache, create_cache
from src.cache.disk import cleanup_stale_tmp
from src.config import Settings, get_settings
from src.downloader.client import AudioDownloader

# Heartbeat file the Docker healthcheck reads. Lives on the container's /tmp
# tmpfs (writable under the read-only rootfs); must stay in sync with the path
# in the bot service healthcheck in docker-compose.yml.
HEARTBEAT_PATH = Path("/tmp/bot_heartbeat")  # noqa: S108 — container tmpfs, by design


async def _error_handler(_update: object, context: CallbackContext) -> None:  # type: ignore[type-arg]
    """Log otherwise-unhandled errors raised while processing updates."""
    logging.getLogger(__name__).error(
        "Unhandled error while processing update", exc_info=context.error
    )


def _record_heartbeat() -> None:
    """Refresh the heartbeat file with the current time."""
    HEARTBEAT_PATH.write_text(str(time.time()))


def _heartbeat_age(now: float | None = None) -> float | None:
    """Seconds since the heartbeat was last refreshed, or None if absent."""
    try:
        mtime = HEARTBEAT_PATH.stat().st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - mtime


def _trigger_restart() -> None:  # pragma: no cover - terminates the process
    """Force a non-zero exit so Docker's restart policy recovers the bot."""
    os._exit(1)


async def _heartbeat_probe(bot: Bot, timeout: float) -> bool:
    """Return True if the bot can reach Telegram within *timeout* seconds."""
    try:
        await asyncio.wait_for(bot.get_me(), timeout=timeout)
    except Exception as exc:
        logging.getLogger(__name__).warning("Heartbeat probe failed: %s", exc)
        return False
    return True


async def _heartbeat_loop(bot: Bot, interval: float, probe_timeout: float) -> None:
    """Refresh the heartbeat each time the bot can still reach Telegram. Runs
    on the event loop; the off-loop watchdog thread handles termination, so a
    failed probe simply stops the refresh and lets the heartbeat go stale."""
    while True:
        if await _heartbeat_probe(bot, probe_timeout):
            _record_heartbeat()
        await asyncio.sleep(interval)


def _watchdog_thread(  # pragma: no cover - loops until it kills the process
    max_stale: float, check_interval: float
) -> None:
    """Force a restart when the heartbeat goes stale. Runs in a plain thread,
    off the event loop, so it still fires even if the loop is fully wedged —
    Docker Compose does not restart merely-unhealthy containers, so an
    in-process terminator is required to actually recover the service."""
    while True:
        time.sleep(check_interval)
        age = _heartbeat_age()
        if age is not None and age > max_stale:
            logging.getLogger(__name__).critical(
                "Heartbeat stale for %.0fs (> %.0fs) — forcing restart",
                age,
                max_stale,
            )
            _trigger_restart()


def _start_heartbeat(
    application: Application[Any, Any, Any, Any, Any, Any], settings: Settings
) -> None:
    """Record an initial heartbeat, launch the async refresher, and start the
    off-loop watchdog thread that terminates the process if it goes stale."""
    _record_heartbeat()
    application.bot_data["_heartbeat_task"] = asyncio.create_task(
        _heartbeat_loop(
            application.bot,
            settings.HEARTBEAT_INTERVAL_SECONDS,
            settings.HEARTBEAT_PROBE_TIMEOUT_SECONDS,
        )
    )
    # Tolerate up to max_failures missed cycles plus one slow-but-successful
    # probe (≤ probe_timeout) before declaring the heartbeat stale, so a small
    # interval relative to the probe timeout cannot kill a healthy instance.
    max_stale = (
        settings.HEARTBEAT_INTERVAL_SECONDS * settings.HEARTBEAT_MAX_FAILURES
        + settings.HEARTBEAT_PROBE_TIMEOUT_SECONDS
    )
    threading.Thread(
        target=_watchdog_thread,
        args=(max_stale, settings.HEARTBEAT_INTERVAL_SECONDS),
        name="heartbeat-watchdog",
        daemon=True,
    ).start()


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


async def post_init(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    """Called after application is initialized. Wire dependencies into bot_data."""
    settings = application.bot_data["settings"]

    # Create cache dir
    settings.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Wire dependencies
    cache = create_cache(settings)
    application.bot_data["cache"] = cache

    # Validate S3 credentials at startup — fail fast rather than silently
    # degrading on the first cache operation hours later.
    if settings.S3_ENABLED and isinstance(cache, CompositeCache):
        _log = logging.getLogger(__name__)
        try:
            await cache.s3.probe()
            _log.info("S3 bucket access verified: %s", settings.S3_BUCKET)
        except Exception as exc:
            _log.error(
                "S3 access check failed — verify credentials and bucket name: %s", exc
            )
            raise

    application.bot_data["downloader"] = AudioDownloader(
        download_dir=settings.CACHE_DIR / "tmp",
        max_file_size_bytes=settings.MAX_FILE_SIZE_MB * 1024 * 1024,
        proxy_url=settings.PROXY_URL,
        cookies_file=settings.COOKIES_FILE,
        download_timeout=settings.DOWNLOAD_TIMEOUT_SECONDS,
    )
    (settings.CACHE_DIR / "tmp").mkdir(parents=True, exist_ok=True)

    # Schedule periodic cleanup of stale tmp files.
    # Always include s3_tmp so leftovers are removed even if S3 is later
    # disabled — cleanup_stale_tmp is a no-op when the directory is absent.
    tmp_dirs = [settings.CACHE_DIR / "tmp", settings.CACHE_DIR / "s3_tmp"]
    max_age = settings.TMP_MAX_AGE_SECONDS
    interval = settings.TMP_CLEANUP_INTERVAL_SECONDS

    async def _cleanup_tmp_job(_context: CallbackContext) -> None:  # type: ignore[type-arg]
        total = 0
        for d in tmp_dirs:
            total += await cleanup_stale_tmp(d, max_age)
        if total:
            logging.getLogger(__name__).info(
                "Scheduled cleanup removed %d stale tmp file(s)", total
            )

    job_queue = application.job_queue
    if job_queue is not None:
        job_queue.run_repeating(
            _cleanup_tmp_job,
            interval=interval,
            first=interval,
            name="cleanup_stale_tmp",
        )

    # Liveness watchdog — restarts the bot if it can no longer reach Telegram.
    _start_heartbeat(application, settings)

    logging.getLogger(__name__).info("Bot initialized and ready")


def build_application(settings: Settings) -> Application[Any, Any, Any, Any, Any, Any]:
    builder = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(30)
        # Fail fast when no pooled connection is free instead of blocking
        # forever; the in-flight edit guard keeps the pool from filling up.
        .pool_timeout(settings.POOL_TIMEOUT_SECONDS)
    )

    # Use local Bot API server if configured
    if settings.TELEGRAM_LOCAL_SERVER_URL:
        local_url = settings.TELEGRAM_LOCAL_SERVER_URL
        builder = builder.base_url(f"{local_url}/bot")
        builder = builder.base_file_url(f"{local_url}/file/bot")
        builder = builder.local_mode(True)

    app = builder.post_init(post_init).build()
    app.bot_data["settings"] = settings

    # Register handlers
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("redownload", handle_redownload))
    app.add_handler(CommandHandler("refresh", handle_refresh))
    app.add_handler(CommandHandler("chapters", handle_chapters))
    app.add_handler(CallbackQueryHandler(handle_chapter_page_callback, pattern=r"^cp:"))
    app.add_handler(MessageHandler(MediaURLFilter(), handle_url))

    # Catch-all so pool/network errors are logged, not silently swallowed.
    app.add_error_handler(_error_handler)

    return app


def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)  # LOG_LEVEL is already UPPER_CASE

    logger = logging.getLogger(__name__)
    logger.info("Starting YouTube Download Bot")

    if not settings.ALLOWED_USER_IDS:
        logger.warning(
            "ALLOWED_USER_IDS is empty — bot will accept requests from ALL "
            "Telegram users. Set ALLOWED_USER_IDS to restrict access."
        )

    app = build_application(settings)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
