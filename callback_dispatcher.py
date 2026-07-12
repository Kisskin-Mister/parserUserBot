import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from database import Database

load_dotenv()

DEFAULT_LOG_LEVEL = "INFO"


def setup_logging() -> None:
    level_name = os.getenv("DISPATCHER_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch parserUserBot callback outbox to OpenClaw")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="Log payloads without sending")
    return parser.parse_args()


class OutboxDispatcher:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.timeout_ms = int(os.getenv("OPENCLAW_CALLBACK_TIMEOUT_MS", "10000"))
        self.gateway_url = os.getenv("OPENCLAW_GATEWAY_URL", "").strip()
        self.gateway_token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
        self.openclaw_bin = self._resolve_openclaw_bin()

    @staticmethod
    def _resolve_openclaw_bin() -> str:
        explicit = os.getenv("OPENCLAW_BIN", "").strip()
        if explicit:
            return explicit
        detected = shutil.which("openclaw")
        if detected:
            return detected
        import platform
        system = platform.system()
        if system == "Darwin":
            for path in [
                "/opt/homebrew/bin/openclaw",
                "/usr/local/bin/openclaw",
            ]:
                if Path(path).exists():
                    return path
            # Check nvm-managed node binaries
            home = Path.home()
            nvm_dir = home / ".nvm" / "versions" / "node"
            if nvm_dir.exists():
                for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
                    candidate = version_dir / "bin" / "openclaw"
                    if candidate.exists():
                        return str(candidate)
        elif system == "Linux":
            for path in [
                "/usr/local/bin/openclaw",
                "/usr/bin/openclaw",
                f"/home/{os.getenv('USER', 'kisskin')}/.local/bin/openclaw",
            ]:
                if Path(path).exists():
                    return path
        return "openclaw"

    def build_command(self, row: dict) -> list[str]:
        params = {
            "sessionKey": row["session_key"],
            "message": row["message"],
            "idempotencyKey": row["idempotency_key"],
        }
        cmd = [
            self.openclaw_bin,
            "gateway",
            "call",
            "chat.send",
            "--json",
            "--timeout",
            str(self.timeout_ms),
            "--params",
            json.dumps(params, ensure_ascii=False),
        ]
        if self.gateway_url:
            cmd.extend(["--url", self.gateway_url])
        if self.gateway_token:
            cmd.extend(["--token", self.gateway_token])
        return cmd

    async def dispatch_row(self, row: dict, dry_run: bool = False) -> bool:
        if dry_run:
            logging.info("DRY RUN dispatch => %s", row["message"])
            return True

        await self.db.mark_callback_outbox_sending(int(row["id"]))
        cmd = self.build_command(row)
        result = subprocess.run(cmd, capture_output=True, text=True)
        stdout_text = result.stdout if isinstance(result.stdout, str) else ""
        stderr_text = result.stderr if isinstance(result.stderr, str) else ""
        response_text = (stdout_text or stderr_text or "").strip()
        if result.returncode == 0:
            await self.db.mark_callback_outbox_sent(int(row["id"]), response_text=response_text)
            if row.get("batch_id"):
                await self.db.mark_callback_batch_sent(int(row["batch_id"]))
            logging.info("Dispatched callback outbox id=%s", row["id"])
            return True

        await self.db.mark_callback_outbox_failed(int(row["id"]), error_text=response_text, response_text=response_text)
        if row.get("batch_id"):
            await self.db.mark_callback_batch_failed(int(row["batch_id"]), response_text)
        logging.error("Dispatch failed for outbox id=%s", row["id"])
        return False


async def run_once(dry_run: bool = False) -> int:
    db = Database()
    await db.init()
    dispatcher = OutboxDispatcher(db)
    rows = await db.get_due_callback_outbox(limit=20)
    if not rows:
        logging.info("No due callback outbox rows")
        return 0
    sent = 0
    for row in rows:
        if await dispatcher.dispatch_row(row, dry_run=dry_run):
            sent += 1
    return sent


def main() -> int:
    setup_logging()
    args = parse_args()
    try:
        result = asyncio.run(run_once(dry_run=args.dry_run))
        return 0 if result >= 0 else 1
    except KeyboardInterrupt:
        logging.info("Stopped by user")
        return 0
    except Exception:
        logging.exception("Dispatcher crashed")
        return 1


if __name__ == "__main__":
    import asyncio
    sys.exit(main())
