"""Entry point for the YouTube music Telegram bot."""
import logging

from app.application import run

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

if __name__ == "__main__":
    logging.getLogger(__name__).info("Bot starting")
    run()
