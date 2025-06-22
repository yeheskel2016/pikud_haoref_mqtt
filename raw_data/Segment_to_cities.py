#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
segment_flatten.py
------------------
Creates a simple id➜name(+parent flag) mapping from Segment.json
Usage:
    python3 segment_flatten.py
"""

import json
from pathlib import Path

IN_FILE  = Path("Segment.json")          # ← adjust if needed
OUT_FILE = Path("segment_clean.json")

def main():
    rows = json.loads(IN_FILE.read_text(encoding="utf-8"))

    # --- find all IDs that are referenced as a parent -----------------
    parent_ids = {row["parent"] for row in rows if row.get("parent")}
    # also respect explicit flag, if some rows have isParent == 1 / true
    parent_ids.update(row["id"] for row in rows
                      if str(row.get("isParent")).lower() in {"1", "true"})

    cleaned = []
    for row in rows:
        name = row["name"]
        if row["id"] in parent_ids:
            name += " (ראשי)"          # suffix for parent segments
        cleaned.append({"id": row["id"], "name": name})

    OUT_FILE.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"✔ Wrote {OUT_FILE} with {len(cleaned):,} records")

if __name__ == "__main__":
    main()
