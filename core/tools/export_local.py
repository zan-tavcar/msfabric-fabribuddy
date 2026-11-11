"""
The Contoso demo's answers as a local file, to preview the app without Fabric.

    core/.venv/bin/python core/tools/export_local.py app/public/local-chunks.json
    cd app && npm run dev:preview

`--live` keeps going: every 30 seconds it re-reads the demo's monitor pages and
rewrites them marked "live", as FabriBuddy_Live does in Fabric — the open page
picks the change up on its next poll (its Live badge, the running runs).

Runs the same steps as a demo notebook run — load the demo, ask every route —
but writes {key: chunks} to a JSON file instead of the SQL database.
"""

import datetime as dt
import json
import sys
import tempfile
import time
from pathlib import Path

from fabribuddy_intenant import export, payloads, runner


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    live = "--live" in sys.argv
    out = Path(args[0] if args else "local-chunks.json")
    work = Path(tempfile.mkdtemp(prefix="fabribuddy-"))
    db_file = runner._configure(work, "", demo=True)
    runner._migrate(db_file)

    from fabribuddy_core import db, demo_org

    session = db.SessionLocal()
    try:
        demo_org._build(session)
    finally:
        session.close()
    answers, stats = export.collect()
    out.write_text(json.dumps({payloads.key(u): payloads.pack(v) for u, v in answers.items()}))
    print(f"{stats['stored']} answers -> {out}")
    every = 30
    while live:
        time.sleep(every)
        pages, _ = export.collect()
        pages["/monitor/live"]["source"] = {"kind": "live", "everySeconds": every}
        out.write_text(json.dumps({payloads.key(u): payloads.pack(v) for u, v in pages.items()}))
        print(f"{dt.datetime.now():%H:%M:%S} live: rewrote the monitor's pages", flush=True)


if __name__ == "__main__":
    main()
