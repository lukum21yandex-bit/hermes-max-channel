#!/usr/bin/env python3
"""
Hermes MAX Bot — entry point.

Run:
    export MAX_TOKEN=***
    python -m src.main
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Coroutine

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv  # type: ignore

dotenv_path = project_root / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)

from src.bot import HermesMaxBot, BotConfig
from src.hermes_worker import HermesWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


async def amain():
    token = os.getenv("MAX_TOKEN")
    if not token:
        logger.error("MAX_TOKEN not set. Set env var or create .env file")
        sys.exit(1)

    allowed = os.getenv("ALLOWED_USER_IDS")
    allowed_ids = None
    if allowed:
        allowed_ids = [int(x.strip()) for x in allowed.split(",") if x.strip()]

    config = BotConfig(
        token=token,
        allowed_user_ids=allowed_ids,
    )

    bot = HermesMaxBot(config)
    worker = HermesWorker(poll_interval=1.0)

    try:
        # Run both concurrently
        logger.info("Starting Hermes MAX Bot + AI Worker...")
        await asyncio.gather(
            bot.run(),
            worker.run(),
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        bot.stop()
        await worker.stop()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(amain())
