import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import valorant_api

DATA_DIR = Path.home() / ".config" / "valorant-shop-tracker"
DB_PATH = DATA_DIR / "tracker.db"

# soft-import for desktop notifications
try:
    import subprocess
    HAS_NOTIFY = True
except ImportError:
    HAS_NOTIFY = False

def _db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            region TEXT NOT NULL DEFAULT 'na',
            access_token TEXT,
            entitlements_token TEXT,
            ssws TEXT,
            expires_at INTEGER,
            created_at INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS wishlist (
            id INTEGER PRIMARY KEY,
            skin_name TEXT NOT NULL UNIQUE,
            added_at INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL,
            skin_name TEXT NOT NULL,
            price INTEGER,
            seen_at INTEGER DEFAULT (unixepoch()),
            UNIQUE(account_id, skin_name, seen_at)
        );
        CREATE INDEX IF NOT EXISTS idx_offers_seen ON offers(seen_at);
    """)
    conn.commit()
    conn.close()

def cmd_add_account(args):
    name = args.name
    region = args.region.lower()
    if region not in ('na', 'eu', 'ap', 'kr'):
        print(f"invalid region: {region}", file=sys.stderr)
        sys.exit(1)
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO accounts (name, region) VALUES (?, ?)",
            (name, region)
        )
        conn.commit()
        print(f"added account '{name}' ({region})")
    except sqlite3.IntegrityError:
        print(f"account '{name}' already exists", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

def cmd_remove_account(args):
    name = args.name
    conn = _db()
    cur = conn.cursor()
    cur.execute("DELETE FROM accounts WHERE name = ?", (name,))
    if cur.rowcount == 0:
        print(f"account '{name}' not found", file=sys.stderr)
        sys.exit(1)
    conn.commit()
    conn.close()
    print(f"removed account '{name}'")

def cmd_list_accounts(args):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT name, region FROM accounts ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("no accounts. add one with --add-account NAME")
        return 0
    for row in rows:
        print(f"{row['name']} ({row['region']})")

def cmd_add_skin(args):
    name = args.skin_name
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO wishlist (skin_name) VALUES (?)", (name,))
        conn.commit()
        print(f"added '{name}' to wishlist")
    except sqlite3.IntegrityError:
        print(f"'{name}' is already on the wishlist", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

def cmd_remove_skin(args):
    name = args.skin_name
    conn = _db()
    cur = conn.cursor()
    cur.execute("DELETE FROM wishlist WHERE skin_name = ?", (name,))
    if cur.rowcount == 0:
        print(f"'{name}' not found in wishlist", file=sys.stderr)
        sys.exit(1)
    conn.commit()
    conn.close()
    print(f"removed '{name}' from wishlist")

def cmd_list_skins(args):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT skin_name FROM wishlist ORDER BY skin_name")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("no skins on wishlist. add one with --add-skin NAME")
        return 0
    for row in rows:
        print(row['skin_name'])

def _load_accounts():
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM accounts ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def _load_wishlist():
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT skin_name FROM wishlist")
    rows = cur.fetchall()
    conn.close()
    return [r['skin_name'] for r in rows]

def _save_tokens(account_id, tokens):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE accounts SET
            access_token = ?,
            entitlements_token = ?,
            ssws = ?,
            expires_at = ?
        WHERE id = ?""",
        (
            tokens.get('access_token'),
            tokens.get('entitlements_token'),
            tokens.get('ssws'),
            tokens.get('expires_at'),
            account_id
        )
    )
    conn.commit()
    conn.close()

def _record_offers(account_id, skins):
    conn = _db()
    cur = conn.cursor()
    now = int(datetime.now(timezone.utc).timestamp())
    for skin in skins:
        cur.execute(
            """INSERT OR IGNORE INTO offers
                (account_id, skin_name, price, seen_at)
                VALUES (?, ?, ?, ?)""",
            (account_id, skin['name'], skin.get('cost', 0), now)
        )
    conn.commit()
    conn.close()

def _notify(title, message):
    if sys.platform == "darwin" and HAS_NOTIFY:
        try:
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}"'
            ], check=False, capture_output=True)
        except Exception:
            pass
    elif sys.platform.startswith("linux") and HAS_NOTIFY:
        try:
            subprocess.run([
                "notify-send", title, message
            ], check=False, capture_output=True)
        except Exception:
            pass
    else:
        print(f"[NOTIFY] {title}: {message}")

def cmd_check(args):
    accounts = _load_accounts()
    if not accounts:
        print("no accounts configured. add one with --add-account NAME")
        return 0
    wishlist = _load_wishlist()
    if not wishlist:
        print("no skins on wishlist. add one with --add-skin NAME")
        return 0

    notify = getattr(args, 'notify', False)
    found_any = False
    for acct in accounts:
        print(f"checking {acct['name']}...")
        try:
            tokens = valorant_api.refresh_auth(acct)
            if tokens:
                _save_tokens(acct['id'], tokens)
                acct.update(tokens)
            skins = valorant_api.fetch_storefront(acct)
            _record_offers(acct['id'], skins)
            matches = [s for s in skins if s['name'] in wishlist]
            if matches:
                found_any = True
                print(f"  MATCH on {acct['name']}:")
                for m in matches:
                    print(f"    - {m['name']} ({m.get('cost', '?')} VP)")
                if notify:
                    for m in matches:
                        _notify("Valorant Shop", f"{m['name']} on {acct['name']}")
            else:
                print(f"  no matches")
        except Exception as e:
            print(f"  failed: {e}", file=sys.stderr)
    return 0

def cmd_notify_test(args):
    _notify("Valorant Shop Tracker", "test notification")
    print("notification sent (or printed if no desktop support)")

def cmd_history(args):
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT a.name, o.skin_name, o.price, o.seen_at
        FROM offers o
        JOIN accounts a ON a.id = o.account_id
        ORDER BY o.seen_at DESC
        LIMIT ?
    """, (args.limit,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("no history yet. run 'check' first.")
        return 0
    for row in rows:
        ts = datetime.fromtimestamp(row['seen_at'], tz=timezone.utc).isoformat()
        print(f"{ts}  {row['name']}  {row['skin_name']}  {row['price']} VP")

def main():
    init_db()
    parser = argparse.ArgumentParser(
        prog="tracker",
        description="track valorant shop rotations across accounts"
    )
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add-account", help="add an account")
    p_add.add_argument("name")
    p_add.add_argument("--region", default="na")
    p_add.set_defaults(func=cmd_add_account)

    p_rem = sub.add_parser("remove-account", help="remove an account")
    p_rem.add_argument("name")
    p_rem.set_defaults(func=cmd_remove_account)

    sub.add_parser("list-accounts", help="list accounts").set_defaults(func=cmd_list_accounts)

    p_add_skin = sub.add_parser("add-skin", help="add skin to wishlist")
    p_add_skin.add_argument("skin_name")
    p_add_skin.set_defaults(func=cmd_add_skin)

    p_rem_skin = sub.add_parser("remove-skin", help="remove skin from wishlist")
    p_rem_skin.add_argument("skin_name")
    p_rem_skin.set_defaults(func=cmd_remove_skin)

    sub.add_parser("list-skins", help="list wishlist").set_defaults(func=cmd_list_skins)

    p_check = sub.add_parser("check", help="check shop now")
    p_check.add_argument("--notify", action="store_true", help="send desktop notification on match")
    p_check.set_defaults(func=cmd_check)

    p_hist = sub.add_parser("history", help="show recent offers")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    sub.add_parser("notify-test", help="test notification").set_defaults(func=cmd_notify_test)

    args = parser.parse_args()
    if args.command is None:
        parser.print_usage()
        sys.exit(2)
    return args.func(args)

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
