"""Bootstrap an operator account: python -m src.meditrace.create_user.

There is no HTTP registration endpoint on purpose (see auth.py). Run this
from an environment with direct access to the target database.
"""

from __future__ import annotations

import argparse
import getpass

from .auth import ROLES, create_user
from .database import SessionLocal, create_schema


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", choices=sorted(ROLES), default="clinician")
    args = parser.parse_args()
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match")
    if len(password) < 12:
        raise SystemExit("Use a password of at least 12 characters")
    create_schema()
    with SessionLocal() as session:
        user = create_user(session, args.email, password, args.role)
    print(f"Created {user.role} account for {user.email}")


if __name__ == "__main__":
    main()
