#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass

from app.session_auth import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a scrypt password hash for MINIGENT_SESSION_CREDENTIALS."
    )
    parser.parse_args()
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if not password:
        raise SystemExit("Password must not be empty")
    print(hash_password(password))


if __name__ == "__main__":
    main()
