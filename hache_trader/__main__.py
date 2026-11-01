import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from .domain import GuardError, Policy
from .reports import report
from .runtime import ProcessLock, Worker
from .safetrade import SafeTrade
from .store import Store
from .telegram import Channel, TelegramAPI, read_secret


def main():
    parser = argparse.ArgumentParser(description="HACHE-TRADER — SIMULACIÓN únicamente")
    parser.add_argument("command", choices=["init", "run", "scan", "status", "pause", "resume", "emergency", "reconcile", "backup", "policy"])
    parser.add_argument("--state-dir", default="runtime")
    parser.add_argument("--policy")
    parser.add_argument("--destination")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()
    policy = Policy(**json.loads(Path(args.policy).read_text())) if args.policy else Policy()
    if args.command == "policy":
        print(json.dumps(asdict(policy), indent=2))
        return
    root = Path(args.state_dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and root.stat().st_mode & 0o077:
        raise GuardError("state_directory_requires_0700")
    if (root / "ledger.sqlite").is_symlink():
        raise GuardError("unsafe_database_path")
    now = int(time.time())
    if args.command == "emergency":
        # Independent interlock is written before waiting for any database transaction.
        (root / "STOP").touch(mode=0o600, exist_ok=True)
    store = Store(root / "ledger.sqlite", policy, now)
    try:
        if args.command in ("init", "reconcile"):
            with ProcessLock(root / "worker.lock"):
                store.reconcile(now)
        elif args.command in ("pause", "resume", "emergency"):
            if args.command == "resume" and (root / "STOP").exists():
                raise GuardError("independent_stop_active")
            store.control(args.command, now)
        elif args.command == "backup":
            if not args.destination:
                raise GuardError("backup_destination_required")
            dest = Path(args.destination).resolve()
            if dest.exists() or dest == root / "ledger.sqlite":
                raise GuardError("backup_refuses_overwrite")
            store.backup(dest)
        elif args.command in ("run", "scan"):
            with ProcessLock(root / "worker.lock"):
                store.reconcile(now)
                api = SafeTrade()
                worker = Worker(store, policy, api, root / "STOP")
                channel = None
                if args.telegram:
                    creds = Path(os.environ.get("CREDENTIALS_DIRECTORY", "credentials"))
                    tg = TelegramAPI(read_secret(creds / "telegram_token"))
                    if tg.call("getWebhookInfo", {}).get("url"):
                        raise GuardError("telegram_webhook_conflict")
                    expected = int(os.environ["HACHE_TRADER_BOT_ID"])
                    if tg.call("getMe", {}).get("id") != expected:
                        raise GuardError("independent_bot_identity_mismatch")
                    channel = Channel(store, tg, int(os.environ["HACHE_TRADER_CHAT_ID"]), int(os.environ["HACHE_TRADER_USER_ID"]), policy)
                while True:
                    success = worker.cycle()
                    if channel:
                        try:
                            channel.poll(int(time.time()))
                        except GuardError:
                            store.control("pause", int(time.time()))
                    if args.command == "scan":
                        if not success:
                            raise GuardError("scan_unavailable_entries_paused")
                        break
                    time.sleep(30)
        print(json.dumps(report(store, int(time.time())), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except (GuardError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"mode": "SIMULACIÓN", "error": str(exc) if isinstance(exc, GuardError) else "configuration_or_storage_unavailable", "live_authorized": False}))
        raise SystemExit(2) from None
