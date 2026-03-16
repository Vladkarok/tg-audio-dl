import logging

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
)

from src.bot.filters import MediaURLFilter
from src.bot.handlers import handle_help, handle_start, handle_url
from src.cache import create_cache
from src.config import get_settings
from src.downloader.client import AudioDownloader


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def post_init(application: Application) -> None:
    """Called after application is initialized. Wire dependencies into bot_data."""
    settings = application.bot_data["settings"]

    # Create cache dir
    settings.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Wire dependencies
    application.bot_data["cache"] = create_cache(settings)
    application.bot_data["downloader"] = AudioDownloader(
        download_dir=settings.CACHE_DIR / "tmp",
        max_file_size_bytes=settings.MAX_FILE_SIZE_MB * 1024 * 1024,
    )
    (settings.CACHE_DIR / "tmp").mkdir(parents=True, exist_ok=True)

    logging.getLogger(__name__).info("Bot initialized and ready")


def build_application(settings) -> Application:
    builder = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(30)
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
    app.add_handler(MessageHandler(MediaURLFilter(), handle_url))

    return app


def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)  # LOG_LEVEL is already UPPER_CASE

    logger = logging.getLogger(__name__)
    logger.info("Starting YouTube Download Bot")

    app = build_application(settings)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
