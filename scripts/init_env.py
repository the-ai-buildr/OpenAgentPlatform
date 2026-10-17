#!/usr/bin/env python3
"""Create a local .env from .env.example with generated secrets.

Run via `make env` / `make setup`. Existing .env files are never overwritten,
and only the placeholder values are substituted — anything the operator has
already edited is left alone.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

PLACEHOLDER = "CHANGE_ME_GENERATED_BY_MAKE_SETUP"
GENERATED = {
    "DB_PASS": 24,
    "OS_SECURITY_KEY": 32,
    "WEBUI_SECRET_KEY": 32,
}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env = root / ".env"
    example = root / ".env.example"

    if env.exists():
        print(".env already exists - leaving it alone.")
        return 0
    if not example.exists():
        print("error: .env.example is missing", file=sys.stderr)
        return 1

    text = example.read_text(encoding="utf-8")
    for key, length in GENERATED.items():
        text = text.replace(f"{key}={PLACEHOLDER}", f"{key}={secrets.token_urlsafe(length)}")

    env.write_text(text, encoding="utf-8")
    env.chmod(0o600)

    print("Created .env with generated DB_PASS, OS_SECURITY_KEY and WEBUI_SECRET_KEY.")
    print("NEXT: add OPENROUTER_API_KEY to .env (and EMBEDDING_API_KEY to enable knowledge).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
