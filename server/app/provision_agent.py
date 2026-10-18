"""Operator-only provisioning: one transaction, one intentional secret output."""
import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from urllib.parse import urlsplit

import asyncpg

from .secrets import read_secret
from .uuid7 import uuid7


class ProvisionError(ValueError):
    """Messages are static and safe to display; never include input values."""


def validate_inputs(driver_slug, driver_name, agent_name):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", driver_slug):
        raise ProvisionError("Driver slug must contain lowercase letters, digits and hyphens")
    if not driver_name.strip() or not agent_name.strip():
        raise ProvisionError("Driver name and agent name must be nonempty")


def validate_server_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in ("ws", "wss") or not url.hostname or url.fragment
                or url.username or url.password or any(c.isspace() for c in value)
                or (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError
    except ValueError:
        raise ProvisionError("Server URL must be a valid ws:// or wss:// URL without embedded credentials") from None


def database_options():
    # Same precedence and password reader as server bootstrap; no bootstrap
    # token required and no schema creation or migration as a provisioning side effect.
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        return {"dsn": dsn}
    password = read_secret("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE")
    if not password or password.startswith(("change-", "replace-")):
        raise ProvisionError("Configure POSTGRES_PASSWORD or DATABASE_URL")
    try:
        port = int(os.environ.get("POSTGRES_PORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ProvisionError("POSTGRES_PORT must be a valid port number") from None
    return {"host": os.environ.get("POSTGRES_HOST", "localhost"), "port": port,
            "database": os.environ.get("POSTGRES_DB", "stintara"),
            "user": os.environ.get("POSTGRES_USER", "stintara"), "password": password}


async def provision_agent(conn, driver_slug, driver_name, agent_name):
    validate_inputs(driver_slug, driver_name, agent_name)
    token = secrets.token_urlsafe(32)
    try:
        async with conn.transaction():
            if await conn.fetchval("SELECT id FROM agents WHERE name=$1", agent_name):
                raise ProvisionError("Agent name already exists; no token was changed")
            driver_id = await conn.fetchval(
                """INSERT INTO drivers(id,slug,display_name) VALUES($1,$2,$3)
                   ON CONFLICT(slug) DO UPDATE SET display_name=EXCLUDED.display_name, updated_at=now()
                   RETURNING id""", uuid7(), driver_slug, driver_name)
            await conn.execute(
                "INSERT INTO agents(id,driver_id,name,token_sha256) VALUES($1,$2,$3,$4)",
                uuid7(), driver_id, agent_name, hashlib.sha256(token.encode("utf-8")).hexdigest())
    except asyncpg.UniqueViolationError:
        # Also protects concurrent attempts; rollback includes the driver upsert.
        raise ProvisionError("Agent name or token already exists; no provisioning changes were saved") from None
    return token


async def provision_from_environment(args):
    conn = await asyncpg.connect(**database_options())
    try:
        return await provision_agent(conn, args.driver_slug, args.driver_name, args.agent_name)
    finally:
        await conn.close()


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid provisioning arguments; use --help.\n")


def cli(argv=None):
    parser = SafeArgumentParser(description="Provision one tester agent. Success prints a one-time secret to stdout.")
    parser.add_argument("--driver-slug", required=True)
    parser.add_argument("--driver-name", required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--server-ws", help="Output a complete tester JSON config instead of a bare token")
    args = parser.parse_args(argv)
    try:
        validate_inputs(args.driver_slug, args.driver_name, args.agent_name)
        if args.server_ws is not None:
            validate_server_url(args.server_ws)
        token = asyncio.run(provision_from_environment(args))
    except ProvisionError as exc:
        parser.exit(1, f"Provisioning failed: {exc}\n")
    except Exception:
        # Driver/connection exceptions can contain SQL, credentials or DSNs.
        parser.exit(1, "Provisioning failed; check database configuration, connectivity and initialized schema.\n")
    if args.server_ws is not None:
        print(json.dumps({"server_ws": args.server_ws, "pitwall_ws": "ws://127.0.0.1:9000?format=json", "token": token}, indent=2))
    else:
        print("Save the one-time token from stdout in the tester's private stintara-agent.json.", file=sys.stderr)
        print(token)


if __name__ == "__main__":
    cli()
