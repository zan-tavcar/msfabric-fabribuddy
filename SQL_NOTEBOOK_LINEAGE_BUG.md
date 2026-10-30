# SQL Notebook Lineage Bug

## Purpose

Document a SQL-format Fabric notebook lineage defect and prepare a focused
upstream pull request. The local fix is implemented; Fabric validation is pending.

All example names and SQL below are fictional. This document excludes actual
workspace, notebook, lakehouse, and table names, tenant and item identifiers,
local file paths, credentials, and business query contents.

## Symptom and Expected Behavior

A SQL notebook creates a Materialized Lake View (MLV), but the Data Catalog
does not show the expected notebook-to-table lineage.

For example, a fictional notebook attached to `example_lakehouse` might contain:

```sql
CREATE OR REPLACE MATERIALIZED LAKE VIEW dbo.example_output
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')
AS
SELECT record_id
FROM dbo.example_input;
```

Expected: collect the notebook definition, parse its SQL, apply the attached
lakehouse context, resolve the target to its catalog table, and record its
source dependency. The write should be classified as `materialized_view`.

## Confirmed Local Findings

The following findings describe the original behavior before the local fix.

### Definition Filtering Excludes SQL Notebooks

In [fabric/definitions.py](core/src/fabribuddy_core/fabric/definitions.py),
`RELEVANT_PARTS["Notebook"]` accepts only `notebook-content.py`.
`get_definition()` filters parts through `is_relevant()` before returning them.

Observed result:

```text
is_relevant("Notebook", "notebook-content.sql") -> False
```

If the API returns a SQL-format notebook part, the collector discards it before
storage or parsing.

### Parser Dispatch Assumes Python Notebook Files

In [collectors/definitions.py](core/src/fabribuddy_core/lineage/collectors/definitions.py),
`_run_parser()` selects only `notebook-content.py` for Notebook items. A notebook
with only a SQL part produces no parse result.

Other notebook-selection paths in the same file also assume Python files.
Review context building and pipeline-related parsing when implementing the fix.

### Notebook Parsing Assumes Python Markers and Cells

In [parsers/notebook.py](core/src/fabribuddy_core/lineage/parsers/notebook.py):

- `_CELL_MARKER` recognizes Python-style `# CELL` and metadata markers.
- `_parse_meta()` reads metadata lines beginning with `#`.
- Cells starting with `%%sql` go to the SQL parser; other cells are parsed
  through Python's `ast.parse()`.

SQL-format exports instead use `-- CELL`, `-- MARKDOWN`, `-- METADATA`, and
`-- META` markers, with native SQL cells that do not require `%%sql`.

A local reproduction using a SQL-format notebook returned one unsplit cell,
empty default-lakehouse metadata, zero reads and writes, and warnings about
missing lakehouse metadata and invalid Python syntax. No original source or
identifying output is included here.

### MLV SQL Support Already Exists

In [parsers/sql.py](core/src/fabribuddy_core/lineage/parsers/sql.py),
`_rewrite_mlv()` rewrites MLV creation statements to CTAS for parsing.
`parse_sql()` labels matching writes as `materialized_view`.

This points to notebook format handling rather than a complete lack of MLV
support. After installing the project in an isolated development environment,
the patched parser produced an MLV write and its source-table references from
a privately checked notebook, with its attached lakehouse context preserved.
No private source or identifiers were added to the repository.

The installed SQL dependency also produces column-tracing warnings for explicit
column projections, including through the existing Python notebook paths.
Column-level compatibility is a separate issue; this change and its regression
tests focus on table-level lineage.

## Local Validation

The synthetic regression tests are in
[test_notebook_lineage.py](core/tests/test_notebook_lineage.py).
They cover definition fetching, SQL metadata and cell numbering, collector
dispatch, pipeline runs, mixed-language cells, Python SQL paths, and resolving
the MLV write into an edge between existing catalog nodes.

Run the focused checks from the repository root on Windows:

```powershell
& ./core/.venv/Scripts/python.exe -m pytest ./core/tests/test_notebook_lineage.py -q
```

Run all repository Python tests with `-m pytest ./core/tests -q` using the same
interpreter. The focused suite currently contains 14 passing tests.

## Remaining Verification

- Confirm the deployed Fabric API returns the same SQL-format part as the Git
  export. The export alone does not establish the API response shape.
- Verify the scan identity has the required workspace and notebook access.
- Verify the deployed catalog reflects the tested attached-lakehouse resolution
  after a scan with the patched package and refreshed definitions.

Inspect API responses privately. Do not attach real notebook definitions,
identifiers, tokens, secrets, or local environment details to a public issue.

## Implementation Checklist

- [x] Retain `notebook-content.sql` definition parts alongside Python parts.
- [x] Carry source language through notebook extraction and parser dispatch.
- [x] Support SQL comment markers and metadata without breaking Python exports.
- [x] Parse native SQL cells without requiring a `%%sql` prefix.
- [x] Preserve attached-lakehouse metadata and reuse table qualification logic.
- [x] Review referenced-notebook and pipeline parsing for Python-only assumptions.
- [x] Increment `PARSER_VERSION` when parser behavior changes.
- [x] Document the full rescan requirement for live verification. A parser-version
  bump cannot restore source text that was never stored.

## Regression Tests

Reuse existing test files and helpers where suitable. Add only minimal synthetic
fixtures, not copies of production notebooks or business SQL.

- [x] Definition filtering retains SQL parts and excludes irrelevant parts.
- [x] Collector dispatch parses a SQL-format notebook.
- [x] Notebook metadata survives later per-cell metadata blocks.
- [x] Markdown is ignored while visible cell numbering remains correct.
- [x] MLV SQL with `TBLPROPERTIES` produces the expected reads and a
      `materialized_view` write, qualified against the attached lakehouse.
- [x] Resolution connects that write to an existing catalog table.
- [x] Existing Python, `%%sql`, and `spark.sql(...)` table lineage is preserved.
- [ ] An updated scanner can fetch and parse a previously skipped SQL notebook.

## Deployment and Pull Request

This fix belongs to the Python scanner, not the frontend. Follow the documented
package update process, deploy the updated scanner, rescan with definitions
refetched, and verify the published lineage. A frontend-only deployment will
not apply the fix.

Build a local test wheel without changing the tracked release package:

```powershell
& ./core/.venv/Scripts/python.exe -m pip wheel ./core --no-deps --wheel-dir ./core/dist
```

The normal deployment notebook downloads the published release, not this local
patch. For a first private Fabric test, use a temporary copy of the scan notebook
and explicitly install the local test wheel after the existing package-install
step. Force reinstallation because the development wheel retains the current
package version. Install it before importing the scanner; use a fresh session
to avoid cached imports. Keep credentials in the existing Key Vault flow.

Set `full_refresh` to `true` for that run so definition fetching is not skipped
based on earlier modification times. Verify the MLV producer and source edges
in the published catalog. A scan writes scanner state and published pages, so
prefer staging or back up the target database before testing against an existing
deployment. Do not replace the scheduled deployment until the test succeeds.

Suggested PR title: **Fix lineage extraction for SQL-format Fabric notebooks**.

Include a synthetic reproduction, regression tests, validation results, and any
rescan requirement. Keep unrelated refactors and frontend changes out of scope.