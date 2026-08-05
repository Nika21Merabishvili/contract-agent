"""Manage the local login store for the web app (add / set-password / list / remove).

The intranet deployment authenticates against a small bcrypt-hashed user store
(there was no SSO to integrate with). This is the admin tool for it -- run it on
the server, as the account that owns the store file. Passwords are read from a
no-echo prompt, never from the command line, so they do not land in shell
history or the process table.

    python manage_users.py add alice
    python manage_users.py passwd alice
    python manage_users.py list
    python manage_users.py remove alice

The store location follows NXIA_USERS_FILE (default instance/users.json); set the
same value here as the server uses.
"""

from __future__ import annotations

import argparse
import getpass
import sys

import security as sec


def _read_new_password(username: str) -> str | None:
    """Prompt (no echo) for a password, confirm it, and enforce the policy.

    Returns the password, or None if the prompts don't match or the policy is
    not met (a message is printed in that case).
    """
    pw = getpass.getpass("New password: ")
    if pw != getpass.getpass("Confirm password: "):
        print("error: passwords did not match.", file=sys.stderr)
        return None
    problem = _policy_problem(pw, username)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return None
    return pw


def _policy_problem(password: str, username: str) -> str | None:
    """A floor, not the whole policy -- align with the company standard via
    NXIA_MIN_PASSWORD_LENGTH and extend here if the standard requires more."""
    if len(password) < sec.Config.MIN_PASSWORD_LENGTH:
        return f"password must be at least {sec.Config.MIN_PASSWORD_LENGTH} characters."
    if password.strip().lower() == username.strip().lower():
        return "password must not be the same as the username."
    return None


def cmd_add(args: argparse.Namespace) -> int:
    if sec.user_exists(args.username):
        print(f"error: user {args.username!r} already exists (use `passwd` to change it).",
              file=sys.stderr)
        return 1
    pw = _read_new_password(args.username)
    if pw is None:
        return 1
    sec.set_user_password(args.username, pw)
    print(f"Added user {args.username!r}.")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    if not sec.user_exists(args.username):
        print(f"error: no such user {args.username!r}.", file=sys.stderr)
        return 1
    pw = _read_new_password(args.username)
    if pw is None:
        return 1
    sec.set_user_password(args.username, pw)
    print(f"Updated password for {args.username!r} (their other sessions were signed out).")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if sec.remove_user(args.username):
        print(f"Removed user {args.username!r}.")
        return 0
    print(f"error: no such user {args.username!r}.", file=sys.stderr)
    return 1


def cmd_list(_args: argparse.Namespace) -> int:
    users = sec.list_users()
    if not users:
        print("(no users yet -- add one with `python manage_users.py add <name>`)")
        return 0
    for name in users:
        print(name)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="create a new user (prompts for a password)")
    p_add.add_argument("username")
    p_add.set_defaults(func=cmd_add)

    p_pw = sub.add_parser("passwd", aliases=["set-password"],
                          help="change a user's password (prompts)")
    p_pw.add_argument("username")
    p_pw.set_defaults(func=cmd_passwd)

    p_rm = sub.add_parser("remove", aliases=["delete"], help="remove a user")
    p_rm.add_argument("username")
    p_rm.set_defaults(func=cmd_remove)

    p_ls = sub.add_parser("list", help="list usernames")
    p_ls.set_defaults(func=cmd_list)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
