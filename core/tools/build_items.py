"""
Write FabriBuddy's Fabric items and its deploy notebook.

    python3 core/tools/build_items.py

    src/FabriBuddy_Scan.Notebook/           the scanner (Python notebook)
    src/Check_FabriBuddy_Version.Notebook/  installed vs latest release
    src/Load_FabriBuddy_E2E.DataPipeline/   the orchestration pipeline you schedule
    scripts/Deploy_FabriBuddy.ipynb         imported once; deploys and updates the above
    config/deployment_config.yaml           item order and workspace folders

Items are in Fabric's Git format (`.platform` + content), so they can also be
synced with Fabric Git integration. Items reference each other through the
placeholder ids below; Deploy_FabriBuddy replaces them with the real ones.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "zan-tavcar/msfabric-fabribuddy"
DOCS = "https://zan-tavcar.github.io/msfabric-fabribuddy"

# Placeholder ids: Deploy_FabriBuddy maps each to the real one in your workspace.
IDS = {
    "workspace": "f1b5c0de-0000-4000-8000-000000000001",
    "FabriBuddy_Scan.Notebook": "f1b5c0de-0000-4000-8000-000000000002",
    "Check_FabriBuddy_Version.Notebook": "f1b5c0de-0000-4000-8000-000000000003",
    "Load_FabriBuddy_E2E.DataPipeline": "f1b5c0de-0000-4000-8000-000000000004",
}

# The pipeline's parameters — the whole configuration, as in FUAM's Load_FUAM_Data_E2E.
PARAMETERS = {
    "key_vault_url": ("string", ""),
    "sp_tenant_id": ("string", ""),
    "sp_client_id": ("string", ""),
    "sp_secret_name": ("string", "fabribuddy-sp-secret"),
    "workspaces": ("string", ""),
    "include_personal": ("bool", False),
    "tenant_name": ("string", ""),
    "full_refresh": ("bool", False),
    "demo": ("bool", False),
    "sql_database_name": ("string", "FabriBuddy_DB"),
}


# --- notebook plumbing --------------------------------------------------------------
def md(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": _lines(text)}


def code(text: str, parameters: bool = False) -> dict:
    meta: dict = {"microsoft": {"language": "python", "language_group": "jupyter_python"}}
    if parameters:
        meta["tags"] = ["parameters"]
    return {"cell_type": "code", "execution_count": None, "metadata": meta, "outputs": [],
            "source": _lines(text)}


def _lines(text: str) -> list[str]:
    return text.strip("\n").splitlines(keepends=True)


def notebook(cells: list[dict]) -> dict:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernel_info": {"name": "jupyter", "jupyter_kernel_name": "python3.11"},
            "kernelspec": {"name": "jupyter", "display_name": "Jupyter"},
            "language_info": {"name": "python"},
            "microsoft": {"language": "python", "language_group": "jupyter_python"},
            "dependencies": {},
        },
        "cells": cells,
    }


def platform(item_type: str, name: str, description: str) -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": item_type, "displayName": name, "description": description},
        "config": {"version": "2.0", "logicalId": IDS[f"{name}.{item_type}"]},
    }


# Reaching FabriBuddy_DB with nothing but what Fabric's Python runtime has
# (requests, pyodbc, notebookutils), as whoever runs the notebook.
BOOTSTRAP = r'''
# FabriBuddy_DB, before the FabriBuddy package is installed. As whoever runs this notebook.
import json, struct, subprocess, sys, time
import requests

FABRIC_API = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = notebookutils.runtime.context["currentWorkspaceId"]


def fabric(method, path, body=None, wait=True):
    """The Fabric REST API as you; waits for long-running operations."""
    headers = {"Authorization": f"Bearer {notebookutils.credentials.getToken('pbi')}"}
    r = requests.request(method, f"{FABRIC_API}{path}", headers=headers, json=body, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} failed ({r.status_code}): {r.text[:400]}")
    if r.status_code == 202 and wait and r.headers.get("Location"):
        location, state = r.headers["Location"], None
        for _ in range(200):
            time.sleep(min(int(r.headers.get("Retry-After", 3) or 3), 10))
            r = requests.get(location, headers=headers, timeout=60)
            state = (r.json() if r.text else {}).get("status")
            if state in ("Succeeded", "Failed", "Undefined"):
                break
        if state != "Succeeded":
            raise RuntimeError(f"{method} {path} did not succeed: {r.text[:400]}")
        result = requests.get(location + "/result", headers=headers, timeout=60)
        return result.json() if result.status_code == 200 and result.text else {}
    return r.json() if r.text else {}


def find_sql_database(name):
    """(item id, server, database) of the SQL database called `name` here, or (None, "", "")."""
    for item in fabric("GET", f"/workspaces/{WORKSPACE_ID}/sqlDatabases").get("value", []):
        if item["displayName"].lower() == name.lower():
            props = fabric("GET", f"/workspaces/{WORKSPACE_ID}/sqlDatabases/{item['id']}").get("properties") or {}
            return item["id"], (props.get("serverFqdn") or "").split(",")[0], props.get("databaseName") or ""
    return None, "", ""


def sql_token():
    return notebookutils.credentials.getToken("https://database.windows.net/")


def sql_connect(server, database):
    import pyodbc

    raw = sql_token().encode("utf-16-le")
    return pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};Server={server},1433;Database={database};"
        "Encrypt=yes;TrustServerCertificate=no",
        attrs_before={1256: struct.pack(f"<I{len(raw)}s", len(raw), raw)},
        timeout=30,
    )


def pip_install(*args):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *args])
'''


def scan_parameters() -> str:
    lines = ["# Set by the pipeline Load_FabriBuddy_E2E (edit its parameters, not these)."]
    for name, (kind, default) in PARAMETERS.items():
        lines.append(f"{name} = {default!r}")
    return "\n".join(lines) + "\n"


SCAN = [
    md(f"""
# FabriBuddy_Scan

Scans the tenant **read-only as the service principal** and stores what the **FabriBuddy**
app shows in `FabriBuddy_DB`. Run by the pipeline **Load_FabriBuddy_E2E**, which holds the
settings — change them there.

[Documentation]({DOCS}/)
"""),
    code(scan_parameters(), parameters=True),
    code(BOOTSTRAP),
    code('''
# Install the FabriBuddy release that Deploy_FabriBuddy stored in the database.
_, SQL_SERVER, SQL_DATABASE = find_sql_database(sql_database_name)
if not SQL_SERVER:
    raise RuntimeError(f"No SQL database called {sql_database_name!r} here. Run Deploy_FabriBuddy first.")
with sql_connect(SQL_SERVER, SQL_DATABASE) as conn:
    row = conn.cursor().execute("SELECT name, version, data FROM dbo.fabribuddy_package").fetchone()
if row is None:
    raise RuntimeError("The FabriBuddy package isn't in the database. Run Deploy_FabriBuddy first.")
wheel = f"/tmp/{row[0]}"
with open(wheel, "wb") as f:
    f.write(bytes(row[2]))
pip_install(wheel)
print(f"FabriBuddy {row[1]}")
'''),
    code('''
# The service principal's secret, from Azure Key Vault (in memory only).
client_secret = ""
if not demo:
    missing = [n for n in ("key_vault_url", "sp_tenant_id", "sp_client_id") if not globals()[n]]
    if missing:
        raise ValueError(f"Set {', '.join(missing)} in the parameters of Load_FabriBuddy_E2E.")
    client_secret = notebookutils.credentials.getSecret(key_vault_url, sp_secret_name)
'''),
    code('''
# Scan and publish.
from fabribuddy_intenant import run

result = run(
    tenant_id=sp_tenant_id,
    client_id=sp_client_id,
    client_secret=client_secret,
    sql_server=SQL_SERVER,
    sql_database=SQL_DATABASE,
    sql_token=sql_token(),
    tenant_name=tenant_name,
    workspaces=[w for w in workspaces.split(",") if w.strip()],
    include_personal=include_personal,
    full=full_refresh,
    demo=demo,
)
print(json.dumps(result, indent=2, default=str)[:4000])
'''),
]

CHECK = [
    md(f"""
# Check_FabriBuddy_Version

Compares the FabriBuddy release installed in `FabriBuddy_DB` with the latest one on GitHub.
To update, import the latest `Deploy_FabriBuddy` notebook and run it.

[How to update]({DOCS}/update/)
"""),
    code('''sql_database_name = "FabriBuddy_DB"
repository = "''' + REPOSITORY + '''"
github_token = ""   # only while the repository is private
''', parameters=True),
    code(BOOTSTRAP),
    code('''
_, server, database = find_sql_database(sql_database_name)
with sql_connect(server, database) as conn:
    row = conn.cursor().execute("SELECT version, saved_at FROM dbo.fabribuddy_package").fetchone()
installed = row[0] if row else None

headers = {"Authorization": f"token {github_token}"} if github_token else {}
latest = requests.get(f"https://raw.githubusercontent.com/{repository}/main/data/current_fabribuddy_version.json",
                      headers=headers, timeout=60).json()["version"]

print(f"Installed: {installed or 'none'}   Latest: {latest}")
print("FabriBuddy is up to date." if installed == latest else
      f"An update is available. See https://zan-tavcar.github.io/msfabric-fabribuddy/update/")
'''),
]


def pipeline() -> dict:
    return {
        "properties": {
            "activities": [
                {
                    "name": "FabriBuddy_Scan",
                    "description": "Scans the tenant read-only and stores what the FabriBuddy app shows.",
                    "type": "TridentNotebook",
                    "dependsOn": [],
                    "policy": {
                        "timeout": "0.12:00:00",
                        "retry": 0,
                        "retryIntervalInSeconds": 30,
                        "secureOutput": False,
                        "secureInput": False,
                    },
                    "typeProperties": {
                        "notebookId": IDS["FabriBuddy_Scan.Notebook"],
                        "workspaceId": IDS["workspace"],
                        "parameters": {
                            name: {
                                "value": {
                                    "value": f"@pipeline().parameters.{name}",
                                    "type": "Expression",
                                },
                                "type": kind,
                            }
                            for name, (kind, _) in PARAMETERS.items()
                        },
                    },
                }
            ],
            "parameters": {
                name: {"type": kind, "defaultValue": default}
                for name, (kind, default) in PARAMETERS.items()
            },
            "annotations": [],
        }
    }


# --- the deploy notebook ---------------------------------------------------------------
DEPLOY_SETTINGS = '''
# ── Service principal ──────────────────────────────────────────────────────────
# Its secret lives in Azure Key Vault; the notebooks read it at run time.
KEY_VAULT_URL = ""        # e.g. "https://my-vault.vault.azure.net/"
SP_TENANT_ID = ""         # Directory (tenant) ID
SP_CLIENT_ID = ""         # Application (client) ID
SP_SECRET_NAME = "fabribuddy-sp-secret"

# ── What to scan ───────────────────────────────────────────────────────────────
WORKSPACES = ""           # workspace IDs, comma-separated; empty = every workspace
INCLUDE_PERSONAL = False  # Report Audience: include personal "My workspace"s
TENANT_NAME = ""          # how the app names your tenant, e.g. "Contoso"

# ── Deployment ─────────────────────────────────────────────────────────────────
SCHEDULE_DAILY_AT_UTC = "05:00"   # daily scan time; "" for no schedule
RUN_AFTER_DEPLOY = True           # start the first scan when deployment finishes
REPOSITORY = "''' + REPOSITORY + '''"
BRANCH = "main"
GITHUB_TOKEN = ""         # only while the repository is private
'''

DEPLOY = [
    md(f"""
#### Welcome to FabriBuddy

This notebook deploys the latest FabriBuddy into the **current workspace**. It works for the
first deployment and for every update.

[How to deploy and configure FabriBuddy]({DOCS}/deploy/)

**What happens in this notebook**
- It downloads the latest FabriBuddy from GitHub.
- It creates the SQL database **FabriBuddy_DB** and stores the FabriBuddy release in it.
- It deploys or updates the notebooks and the pipeline **Load_FabriBuddy_E2E**, sorted into
  folders.
- It schedules the pipeline and starts the first scan.

**Before you run it:** fill in the service principal in the next cell
([prerequisites]({DOCS}/deploy/#before-you-start)).
On an update, leave it empty: the settings already in the pipeline are kept.
"""),
    code(DEPLOY_SETTINGS, parameters=True),
    md("### Source files"),
    code('''
import base64, json, os, re, shutil, subprocess, sys, tempfile, time, zipfile
from io import BytesIO
import requests

subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "pyyaml"])
import yaml

# Download this repository (src, config, data, release) into a temporary folder.
headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
archive = requests.get(f"https://api.github.com/repos/{REPOSITORY}/zipball/{BRANCH}", headers=headers, timeout=300)
archive.raise_for_status()
SOURCE = tempfile.mkdtemp(prefix="fabribuddy-")
with zipfile.ZipFile(BytesIO(archive.content)) as z:
    for name in z.namelist():
        rel = name.split("/", 1)[1] if "/" in name else ""
        if rel.startswith(("src/", "config/", "data/", "release/")) and not name.endswith("/"):
            target = os.path.join(SOURCE, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(z.read(name))
config = yaml.safe_load(open(os.path.join(SOURCE, "config", "deployment_config.yaml")))
release = json.load(open(os.path.join(SOURCE, "data", "current_fabribuddy_version.json")))
print(f"FabriBuddy {release['version']} from {REPOSITORY}@{BRANCH}")
'''),
    code(BOOTSTRAP),
    code('''
WORKSPACE_NAME = fabric("GET", f"/workspaces/{WORKSPACE_ID}")["displayName"]
print(f"Deploying into: {WORKSPACE_NAME}")


def item_id(name):
    """The id of item `name` ("FabriBuddy_Scan.Notebook") in this workspace, or None."""
    display, kind = name.rsplit(".", 1)
    for item in fabric("GET", f"/workspaces/{WORKSPACE_ID}/items").get("value", []):
        if item["displayName"] == display and item["type"] == kind:
            return item["id"]
    return None


def import_item(name, folder, existing_id):
    """Create item `name` from the files in `folder`, or update its definition."""
    display, kind = name.rsplit(".", 1)
    parts = []
    for root, _, files in os.walk(folder):
        for file in files:
            path = os.path.join(root, file)
            parts.append({
                "path": os.path.relpath(path, folder).replace(os.sep, "/"),
                "payload": base64.b64encode(open(path, "rb").read()).decode(),
                "payloadType": "InlineBase64",
            })
    definition = {"parts": parts}
    if kind == "Notebook":
        definition["format"] = "ipynb"
    if existing_id:
        fabric("POST", f"/workspaces/{WORKSPACE_ID}/items/{existing_id}/updateDefinition?updateMetadata=true",
               {"definition": definition})
    else:
        fabric("POST", f"/workspaces/{WORKSPACE_ID}/items",
               {"displayName": display, "type": kind, "definition": definition})
'''),
    md("### 1. FabriBuddy_DB"),
    code('''
db_id, SQL_SERVER, SQL_DATABASE = find_sql_database(config["sql_database"])
if db_id is None:
    print(f"Creating {config['sql_database']} …")
    fabric("POST", f"/workspaces/{WORKSPACE_ID}/sqlDatabases", {
        "displayName": config["sql_database"],
        "description": "FabriBuddy: what the scan found, and what the FabriBuddy app shows.",
    })
for _ in range(60):
    db_id, SQL_SERVER, SQL_DATABASE = find_sql_database(config["sql_database"])
    if SQL_SERVER:
        break
    time.sleep(10)
if not SQL_SERVER:
    raise RuntimeError(f"{config['sql_database']} isn't ready yet. Wait a minute and run this notebook again.")
print(f"✓ {config['sql_database']}")
'''),
    md("### 2. The FabriBuddy release"),
    code('''
wheel = os.path.join(SOURCE, release["wheel"])
pip_install(wheel)
from fabribuddy_intenant import sqldb

with sql_connect(SQL_SERVER, SQL_DATABASE) as conn:
    sqldb.ensure_schema(conn)
    sqldb.save_package(conn, os.path.basename(wheel), release["version"], open(wheel, "rb").read())
print(f"✓ FabriBuddy {release['version']} stored in {config['sql_database']}")
'''),
    md("### 3. Notebooks and the pipeline"),
    code('''
SETTINGS = {
    "key_vault_url": KEY_VAULT_URL, "sp_tenant_id": SP_TENANT_ID, "sp_client_id": SP_CLIENT_ID,
    "sp_secret_name": SP_SECRET_NAME, "workspaces": WORKSPACES, "include_personal": INCLUDE_PERSONAL,
    "tenant_name": TENANT_NAME,
}
mapping = {config["ids"]["workspace"]: WORKSPACE_ID}


def pipeline_settings(path, existing_id):
    """Pipeline parameter defaults: kept on an update, filled from this notebook on the first deploy."""
    content = json.load(open(path))
    params = content["properties"]["parameters"]
    current = {}
    if existing_id:
        definition = fabric("POST", f"/workspaces/{WORKSPACE_ID}/items/{existing_id}/getDefinition")
        for part in definition.get("definition", {}).get("parts", []):
            if part["path"] == "pipeline-content.json":
                import base64
                current = json.loads(base64.b64decode(part["payload"]))["properties"].get("parameters", {})
    for name, spec in params.items():
        if name in current:
            spec["defaultValue"] = current[name].get("defaultValue", spec["defaultValue"])
        elif name in SETTINGS and SETTINGS[name] not in ("", None):
            spec["defaultValue"] = SETTINGS[name]
    json.dump(content, open(path, "w"), indent=2)


for name in config["items"]:
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(SOURCE, "src", name), os.path.join(work, name))
    existing = item_id(name)
    for root, _, files in os.walk(os.path.join(work, name)):
        for file in files:
            path = os.path.join(root, file)
            text = open(path, encoding="utf-8").read()
            for old, new in mapping.items():
                text = text.replace(old, new)
            open(path, "w", encoding="utf-8").write(text)
    if name.endswith(".DataPipeline"):
        pipeline_settings(os.path.join(work, name, "pipeline-content.json"), existing)
    import_item(name, os.path.join(work, name), existing)
    new_id = item_id(name)
    mapping[config["ids"][name]] = new_id
    print(f"✓ {name} {'updated' if existing else 'created'}")
'''),
    md("### 4. Folders"),
    code('''
for folder, names in config["folders"].items():
    found = fabric("GET", f"/workspaces/{WORKSPACE_ID}/folders").get("value", [])
    folder_id = next((f["id"] for f in found if f["displayName"] == folder), None)
    if folder_id is None:
        folder_id = fabric("POST", f"/workspaces/{WORKSPACE_ID}/folders", {"displayName": folder})["id"]
    ids = [i for i in (item_id(n) for n in names) if i]
    if ids:
        fabric("POST", f"/workspaces/{WORKSPACE_ID}/items/bulkMove", {"targetFolderId": folder_id, "items": ids})
    print(f"✓ {folder}: {', '.join(n.rsplit('.', 1)[0] for n in names)}")
'''),
    md("### 5. Schedule and first scan"),
    code('''
import datetime as dt

pipeline_id = item_id("Load_FabriBuddy_E2E.DataPipeline")
if SCHEDULE_DAILY_AT_UTC:
    path = f"/workspaces/{WORKSPACE_ID}/items/{pipeline_id}/jobs/Pipeline/schedules"
    if fabric("GET", path).get("value"):
        print("✓ Schedule: already set (left as it is)")
    else:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        fabric("POST", path, {"enabled": True, "configuration": {
            "type": "Daily", "times": [SCHEDULE_DAILY_AT_UTC], "localTimeZoneId": "UTC",
            "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "endDateTime": (now + dt.timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%S"),
        }})
        print(f"✓ Schedule: Load_FabriBuddy_E2E daily at {SCHEDULE_DAILY_AT_UTC} UTC")

if RUN_AFTER_DEPLOY:
    fabric("POST", f"/workspaces/{WORKSPACE_ID}/items/{pipeline_id}/jobs/instances?jobType=Pipeline", wait=False)
    print("✓ First scan started: open Load_FabriBuddy_E2E → View run history (10–30 minutes).")
'''),
    md("### 6. The FabriBuddy app"),
    code('''
print(f"""
Last step: add the FabriBuddy app from your computer, once (Node.js 20+):

  git clone https://github.com/{REPOSITORY}.git
  cd {REPOSITORY.split('/')[1]}/app
  npm install
  npx rayfin login
  npm run connect -- {WORKSPACE_ID} {db_id}
  npx rayfin up --workspace-id {WORKSPACE_ID}

Then open "fabribuddy" in this workspace.
Guide: https://zan-tavcar.github.io/msfabric-fabribuddy/deploy/#add-the-app
""")
'''),
]

CONFIG = {
    "sql_database": "FabriBuddy_DB",
    # Deployment order: a pipeline after the notebooks it runs.
    "items": [
        "FabriBuddy_Scan.Notebook",
        "Check_FabriBuddy_Version.Notebook",
        "Load_FabriBuddy_E2E.DataPipeline",
    ],
    "folders": {
        "Scan": ["Load_FabriBuddy_E2E.DataPipeline", "FabriBuddy_Scan.Notebook"],
        "Storage": ["FabriBuddy_DB.SQLDatabase"],
        "Maintenance": ["Check_FabriBuddy_Version.Notebook", "Deploy_FabriBuddy.Notebook"],
    },
    "ids": IDS,
}


def write(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content, indent=2) + "\n"
    path.write_text(text)
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    src = ROOT / "src"
    write(src / "FabriBuddy_Scan.Notebook" / ".platform",
          platform("Notebook", "FabriBuddy_Scan", "Scans the tenant read-only and stores what the FabriBuddy app shows."))
    write(src / "FabriBuddy_Scan.Notebook" / "notebook-content.ipynb", notebook(SCAN))
    write(src / "Check_FabriBuddy_Version.Notebook" / ".platform",
          platform("Notebook", "Check_FabriBuddy_Version", "Compares the installed FabriBuddy release with the latest."))
    write(src / "Check_FabriBuddy_Version.Notebook" / "notebook-content.ipynb", notebook(CHECK))
    write(src / "Load_FabriBuddy_E2E.DataPipeline" / ".platform",
          platform("DataPipeline", "Load_FabriBuddy_E2E", "FabriBuddy's orchestration pipeline: schedule this one."))
    write(src / "Load_FabriBuddy_E2E.DataPipeline" / "pipeline-content.json", pipeline())
    write(ROOT / "scripts" / "Deploy_FabriBuddy.ipynb", notebook(DEPLOY))
    import yaml  # noqa: PLC0415 — a maintainer tool

    write(ROOT / "config" / "deployment_config.yaml",
          "# FabriBuddy's items, their deployment order, workspace folders and placeholder ids.\n"
          "# Read by scripts/Deploy_FabriBuddy.ipynb; written by core/tools/build_items.py.\n"
          + yaml.safe_dump(CONFIG, sort_keys=False))


if __name__ == "__main__":
    main()
