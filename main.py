
"""Community Copilot application entrypoint.

This module starts the Discord community management bot.

Usage:
    Windows:
        .venv\\Scripts\\python.exe main.py

    Linux/macOS:
        .venv/bin/python main.py

Configuration:
    Set DISCORD_TOKEN in your local .env file.

Security:
    Bot credentials must never be logged, printed, or committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `src/` importable without installing the package (keeps fresh-machine
# setup to: create venv, pip install, copy .env, run).
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main() -> int:
    import discord
    from pydantic import ValidationError

    from copilot.bot import CommunityCopilot
    from copilot.config import Settings
    from copilot.errors import ConfigError
    from copilot.util.logging import register_secret, setup_logging

    try:
        settings = Settings()
    except ValidationError as exc:
        print("Configuration error:", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())) or "settings"
            print(f"  - {location}: {error.get('msg', 'invalid value')}", file=sys.stderr)
        print("Copy .env.example to .env and fill in the missing values.", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.logs_dir)
    register_secret(settings.token)

    try:
        settings.validate_for_run()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    for secret in (settings.brave_search_api_key, settings.tavily_api_key):
        value = secret.get_secret_value().strip()
        if value:
            register_secret(value)

    bot = CommunityCopilot(settings)
    try:
        bot.run(settings.token, log_handler=None)  # logging is already configured
    except discord.LoginFailure:
        print(
            "Discord rejected the token. Check DISCORD_TOKEN in your .env — and never share it.",
            file=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        print("Shutting down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
