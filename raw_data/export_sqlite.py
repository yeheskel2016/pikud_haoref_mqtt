#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_sqlite_full.py  –  robust, loss-free table dumper
"""

import sys, json, sqlite3, pathlib, traceback, contextlib

def dump_table(conn: sqlite3.Connection, table: str, out_dir: pathlib.Path) -> None:
    # ── make sure the table exists ────────────────────────────────────────────
    ok = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,)
    ).fetchone()
    if not ok:
        print(f"✖  {table}: not found – skipped")
        return

    out_path = out_dir / f"{table}.json"
    row_count = 0
    first = True

    with out_path.open("w", encoding="utf-8") as fp:
        fp.write("[\n")
        cur = conn.execute(f"SELECT * FROM {table}")
        for row in cur:                       # one pass – never skip rows
            if not first:
                fp.write(",\n")
            json.dump(dict(row), fp, ensure_ascii=False)
            first = False
            row_count += 1
        fp.write("\n]\n")

    size_kb = out_path.stat().st_size / 1024
    print(f"✔  {table:35s} → {out_path.name}  ({row_count:,} rows, {size_kb:,.1f} kB)")

def main(db_path: str, tables: list[str]) -> None:
    db = pathlib.Path(db_path).expanduser().resolve()
    if not db.is_file():
        sys.exit(f"SQLite file not found: {db}")
    out_dir = db.parent

    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        for t in tables:
            try:
                dump_table(conn, t, out_dir)
            except Exception:
                print(f"‼  {t}: export FAILED\n{traceback.format_exc()}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("Usage: python export_sqlite_full.py DB_PATH TABLE [TABLE …]")
    main(sys.argv[1], sys.argv[2:])
