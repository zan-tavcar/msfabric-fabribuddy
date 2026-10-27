"""
The Contoso demo's answers as a local file, to preview the app without Fabric.

    core/.venv/bin/python core/tools/export_local.py app/public/local-chunks.json
    cd app && npm run dev:preview

Runs the same steps as a demo notebook run — load the demo, ask every route —
but writes {key: chunks} to a JSON file instead of the SQL database.
"""

import json
import sys
import tempfile
from pathlib import Path

from fabribuddy_intenant import export, payloads, runner


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "local-chunks.json")
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


if __name__ == "__main__":
    main()
