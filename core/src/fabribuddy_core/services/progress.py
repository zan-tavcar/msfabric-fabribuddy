"""
Live progress of a running scan pass, so the UI can say what is being read.

Held in memory, not in the database. There is one app instance (scans run
in-process), and a progress write from inside a collector would be a second
writer racing the collector's own open transaction, which SQLite answers with
"database is locked". The durable record of a pass is its `scan_pass` row,
written at step boundaries by the pass itself; this module only answers "what
is it doing right now".

A step reports sub-progress through `tick(done, total, detail)` without being
handed anything: the pass sets the current tenant in a context variable, and a
collector running outside a pass (a test, a one-off re-parse) ticks into
nothing.
"""

import contextvars
import threading
import time

_lock = threading.Lock()
_live: dict[str, dict] = {}
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fabribuddy_pass_tenant", default=None
)

# What each step reads, in the words the progress bar shows.
STEP_LABELS = {
    ("lineage", "scanner"): "Reading the workspace inventory (Scanner API)",
    ("lineage", "items"): "Listing items and lakehouse tables",
    ("lineage", "definitions"): "Reading notebook, pipeline and dataflow code",
    ("lineage", "warehouse_sql"): "Reading warehouse SQL",
    ("lineage", "parse"): "Parsing code into lineage",
    ("lineage", "delta"): "Reading Delta logs",
    ("lineage", "jobs"): "Reading job run history",
    ("lineage", "access"): "Resolving who can read each table",
    ("audience", "scan"): "Reading reports and models",
    ("audience", "permissions"): "Reading report permissions and groups",
    ("audience", "activity"): "Reading the audit log",
    ("audience", "licenses"): "Reading licences",
}


def begin(
    tenant_id: str, pass_id: str, steps: list[tuple[str, str]], started_at: str
) -> None:
    """`steps` is the whole pass in order: (product, step) pairs."""
    per_product: dict[str, int] = {}
    for product, _name in steps:
        per_product[product] = per_product.get(product, 0) + 1
    with _lock:
        _live[tenant_id] = {
            "passId": pass_id,
            "startedAt": started_at,
            "stepsTotal": len(steps),
            "perProduct": per_product,
            "stepsDone": 0,
            "product": None,
            "step": None,
            "label": "Starting…",
            "done": None,
            "total": None,
            "detail": "",
            "stepStartedAt": time.time(),
        }
    _current.set(tenant_id)


def step(tenant_id: str, product: str, step_name: str, steps_done: int) -> None:
    with _lock:
        live = _live.get(tenant_id)
        if live is None:
            return
        live.update(
            product=product,
            step=step_name,
            label=STEP_LABELS.get((product, step_name), step_name),
            stepsDone=steps_done,
            done=None,
            total=None,
            detail="",
            stepStartedAt=time.time(),
        )


def tick(done: int, total: int, detail: str = "") -> None:
    """Sub-progress inside the current step (45 of 200 definitions)."""
    tenant_id = _current.get()
    if tenant_id is None:
        return
    with _lock:
        live = _live.get(tenant_id)
        if live is not None:
            live.update(done=done, total=total, detail=detail[:200])


def end(tenant_id: str) -> None:
    with _lock:
        _live.pop(tenant_id, None)
    _current.set(None)


def snapshot(tenant_id: str) -> dict | None:
    """The running pass for this tenant, with an overall percentage, or None."""
    with _lock:
        live = dict(_live.get(tenant_id) or {})
    if not live:
        return None
    total = max(1, live["stepsTotal"])
    fraction = 0.0
    if live["done"] is not None and live["total"]:
        fraction = min(1.0, live["done"] / live["total"])
    live["percent"] = round(100 * (live["stepsDone"] + fraction) / total)
    live["stepSeconds"] = round(time.time() - live.pop("stepStartedAt"))
    # Each product's own count and state, in pass order, so one line can show
    # both: "Data Catalog 8/8 done · Report Audience 2/4 running".
    products, before = [], 0
    for product, count in live.pop("perProduct").items():
        done = min(count, max(0, live["stepsDone"] - before))
        state = (
            "running"
            if product == live.get("product")
            else "done"
            if done >= count
            else "waiting"
        )
        products.append({"product": product, "steps": count, "done": done, "state": state})
        before += count
    live["products"] = products
    return live
