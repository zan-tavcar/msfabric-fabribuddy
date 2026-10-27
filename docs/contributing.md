# Contributing

FabriBuddy is MIT licensed, and issues and pull requests are welcome on
[GitHub](https://github.com/zan-tavcar/msfabric-fabribuddy).

## The repository

```text
core/
  src/fabribuddy_core/      the scanner and page logic
  src/fabribuddy_intenant/  the in-tenant runner: SQL database, publishing
  tools/build_items.py      writes src/, scripts/ and config/
  tools/export_local.py     the Contoso demo as a local file, for the app preview
  tests/
app/
  src/ui/                   the interface
  src/fabric/               Fabric sign-in and reading FabriBuddy_DB
  rayfin/                   the Fabric App definition and its database connector
src/                        the Fabric items the deploy notebook imports
scripts/Deploy_FabriBuddy.ipynb
config/deployment_config.yaml
data/current_fabribuddy_version.json
release/                    the package the deploy notebook installs
docs/                       this site (MkDocs Material)
```

## Common tasks

```bash
# Python
cd core && python3 -m venv .venv && .venv/bin/pip install -e . pytest pyyaml
.venv/bin/python -m pytest tests

# Fabric items and the deploy notebook (after changing build_items.py)
core/.venv/bin/python core/tools/build_items.py

# The app on the Contoso demo, without Fabric
core/.venv/bin/python core/tools/export_local.py app/public/local-chunks.json
cd app && npm install && npm run dev:preview

# A release
cd core && .venv/bin/pip wheel . --no-deps -w ../release
# then update data/current_fabribuddy_version.json (wheel file name and version)

# This site
pip install mkdocs-material
mkdocs serve
```

The site is published to GitHub Pages by `.github/workflows/docs.yml` on every push to `main` that
changes `docs/` or `mkdocs.yml`.

## How the app reads its data

Each answer is stored under the SHA-256 of its request, gzipped and base64-encoded, in
4,000-character chunks. The two halves are `core/src/fabribuddy_intenant/payloads.py` and
`app/src/fabric/payloads.ts`; change them together.
