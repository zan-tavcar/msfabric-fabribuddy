"""
Copy the SaaS backend into this package as `fabribuddy_core`.

The SaaS repository (`../Fabric Lineage/backend`) is the one source of the
parsers, collectors, clients and page logic; this edition runs the same code
inside a Fabric notebook. Run it before building a release:

    python core/sync_from_saas.py [path/to/Fabric Lineage]

It replaces `src/fabribuddy_core/` entirely, so never edit that folder by hand;
in-tenant code lives in `src/fabribuddy_intenant/`.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "src" / "fabribuddy_core"
DEFAULT_SAAS = HERE.parent.parent / "Fabric Lineage"

SKIP_DIRS = {"__pycache__"}
# The SaaS's own business: the web entry point, accounts and members, billing,
# e-mail, the operator console and its analytics. The in-tenant edition serves
# its pages through fabribuddy_intenant.api (lineage, audience, scans only), so
# none of these is imported; leaving them out keeps them out of the package.
EXCLUDE = {
    "main.py",
    "scheduler.py",
    "jobs.py",
    "logging_config.py",
    "billing.py",
    "paddle.py",
    "mailer.py",
    "routers/admin.py",
    "routers/auth.py",
    "routers/billing.py",
    "routers/members.py",
    "routers/platform.py",
    "routers/tenants.py",
    "services/attribution.py",
    "services/costs.py",
    "services/demo_stats.py",
    "services/infra.py",
    "services/microsoft_signin.py",
    "services/web_traffic.py",
}


def main() -> None:
    saas = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAAS
    backend = saas / "backend"
    if not (backend / "app" / "main.py").exists():
        sys.exit(f"Not the SaaS repository: {saas}")

    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(
        backend / "app",
        TARGET,
        ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"),
    )
    for rel in EXCLUDE:
        (TARGET / rel).unlink(missing_ok=True)
    # Alembic owns the schema, here as in the SaaS: the migrations travel with
    # the package and are run by the notebook against its working database.
    migrations = TARGET / "migrations"
    shutil.copytree(
        backend / "migrations",
        migrations,
        ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"),
    )
    for path in migrations.rglob("*.py"):
        text = path.read_text()
        fixed = re.sub(r"\bfrom app\b", "from fabribuddy_core", text)
        fixed = re.sub(r"\bimport app\b", "import fabribuddy_core", fixed)
        if fixed != text:
            path.write_text(fixed)

    commit = subprocess.run(
        ["git", "-C", str(saas), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    (TARGET / "SOURCE").write_text(f"Fabric Lineage backend @ {commit or 'unknown'}\n")
    print(f"Copied the SaaS backend @ {commit or '?'} into {TARGET.relative_to(HERE)}")


if __name__ == "__main__":
    main()
