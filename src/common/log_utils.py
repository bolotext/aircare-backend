"""
Logging utilities for the voice agent system.
"""
import logging
import colorlog
from typing import Optional


def setup_logger(logger_name: str, debug_color="white", info_color="green", log_file: Optional[str] = None,):
    color_handler = colorlog.StreamHandler()
    log_colors = {
        "DEBUG": debug_color,
        "INFO": info_color,
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold_red",
    }
    color_formatter = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors=log_colors,
    )
    color_handler.setFormatter(color_formatter)

    # Get logger and clear existing handlers
    logger = logging.getLogger(logger_name)
    if logger.hasHandlers():
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()

    logger.setLevel(logging.DEBUG)
    logger.addHandler(color_handler)

    # Optional: file handler for per-session logs
    if log_file:
        file_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler = logging.FileHandler(log_file, mode="a")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger