"""The in-tenant edition's own code: the storage contract and a demo export."""

import json
import subprocess
import sys
from pathlib import Path

from fabribuddy_intenant import payloads
from fabribuddy_intenant.export import _selector, urls


def test_keys_ignore_parameter_order_and_encoding():
    a = "/lineage/graph?focus=lh%3Aa%3Ab%3Adbo.t&up=25&down=25"
    b = "/lineage/graph?down=25&up=25&focus=lh:a:b:dbo.t"
    assert payloads.key(a) == payloads.key(b)
    assert payloads.key("/lineage/catalog?") == payloads.key("/lineage/catalog")
    assert payloads.normalize("/lineage/nodes/lh%3Aa%3Ab%3Asales.%C3%A9t%C3%A9") == (
        "/lineage/nodes/lh:a:b:sales.été"
    )


def test_packed_answers_come_back_whole_in_bounded_chunks():
    big = {"rows": [{"id": f"lh:{i}", "name": "x" * 50} for i in range(20000)]}
    chunks = payloads.pack(big)
    assert len(chunks) > 1 and all(len(c) <= payloads.CHUNK for c in chunks)
    assert payloads.unpack(chunks) == big


def test_view_in_lineage_selector_matches_the_frontend():
    assert _selector("lh:a:b:dbo.t") == "+id:lh:a:b:dbo.t+"
    assert _selector("sm:a b") == '+id:"sm:a b"+'
    every = urls(["lh:a:b:dbo.t"], ["item:a:b"])
    assert "/lineage/nodes/lh%3Aa%3Ab%3Adbo.t/code" in every
    assert "/lineage/items/item%3Aa%3Ab" in every


def test_the_demo_publishes_every_page_of_both_products(tmp_path):
    # A fresh process: fabribuddy_core reads its settings once, on import.
    out = tmp_path / "chunks.json"
    tool = Path(__file__).resolve().parents[1] / "tools" / "export_local.py"
    subprocess.run([sys.executable, str(tool), str(out)], check=True, capture_output=True)
    stored = json.loads(out.read_text())
    catalog = payloads.unpack(stored[payloads.key("/lineage/catalog")])
    assert len(catalog["rows"]) > 5
    assert payloads.unpack(stored[payloads.key("/auth/config")])["inTenant"] is True
    assert payloads.unpack(stored[payloads.key("/audience/users")])
    assert payloads.key("/lineage/graph") in stored

