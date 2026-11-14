import logging
from logging.handlers import RotatingFileHandler

from .paths import log_dir


def configure_logging(debug: bool = False) -> None:
    handler = RotatingFileHandler(
        log_dir() / "guardian.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
