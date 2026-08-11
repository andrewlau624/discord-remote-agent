#!/usr/bin/env python3
"""Entrypoint: load config and start the Discord bot."""

from __future__ import annotations

from dra.bot import run
from dra.config import Config


def main() -> None:
    config = Config.load()
    run(config)


if __name__ == "__main__":
    main()
