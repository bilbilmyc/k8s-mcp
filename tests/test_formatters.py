"""Tests for formatters and safety helpers (no cluster required)."""
from __future__ import annotations

from k8s_mcp.formatters import (
    _compact,
    describe,
    mask_secret_data,
    short_table,
    to_yaml,
)


def test_to_yaml_basic():
    obj = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p"}}
    out = to_yaml(obj)
    assert "kind: Pod" in out
    assert "name: p" in out


def test_mask_secret_data_masks_data_and_stringdata():
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "creds"},
        "data": {"password": "YWRtaW4="},
        "stringData": {"token": "abc"},
    }
    masked = mask_secret_data(secret)
    assert masked["data"] == {"password": "***"}
    assert masked["stringData"] == {"token": "***"}
    # non-secret fields untouched
    assert masked["metadata"]["name"] == "creds"


def test_mask_secret_data_non_secret_untouched():
    cm = {"kind": "ConfigMap", "data": {"k": "v"}}
    assert mask_secret_data(cm) == cm


def test_short_table_empty():
    assert short_table([], ["NAME"]) == "(empty)"


def test_short_table_columns_and_alignment():
    rows = [
        {"NAME": "a", "STATUS": "Running"},
        {"NAME": "longer-name", "STATUS": "Pending"},
    ]
    out = short_table(rows, ["NAME", "STATUS"])
    lines = out.split("\n")
    header = lines[0].rstrip()
    assert header.lstrip().startswith("|")
    assert header.rstrip().endswith("|")
    assert "a" in lines[2]
    assert "longer-name" in lines[3]


def test_short_table_separator_row():
    """Line 2 is the markdown separator: pure `---` between boundary
    pipes — no cell text. This row is what `notifier._parse_markdown_table`
    keys on for table detection."""
    rows = [{"NAME": "a", "STATUS": "Running"}]
    out = short_table(rows, ["NAME", "STATUS"])
    sep = out.split("\n")[1]
    assert sep.count("|") == 3
    assert "NAME" not in sep and "STATUS" not in sep
    assert "---" in sep


def test_short_table_escapes_pipes_in_cell_values():
    """A literal `|` inside a cell value would break table parsing — it
    gets escaped to `\\|` so the column structure stays intact."""
    rows = [{"MSG": "alpha | bravo", "TAG": "x"}]
    out = short_table(rows, ["MSG", "TAG"])
    body = out.split("\n")[-1]
    assert r"alpha \| bravo" in body
    border_pipes = body.replace(r"\|", "").count("|")
    assert border_pipes == 3


def test_short_table_flattens_newlines_in_cell_values():
    """Markdown pipe-table cells can't span multiple lines; embedded
    `\\n` is squashed to a space so the row stays one line."""
    rows = [{"MSG": "line1\nline2", "TAG": "x"}]
    out = short_table(rows, ["MSG", "TAG"])
    body = out.split("\n")[-1]
    assert "\n" not in body
    assert "line1 line2" in body


def test_describe_basic():
    obj = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "web",
            "namespace": "default",
            "labels": {"app": "web"},
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": {"replicas": 3, "selector": {"matchLabels": {"app": "web"}}},
        "status": {"readyReplicas": 3, "replicas": 3},
    }
    out = describe(obj)
    assert "Name:       web" in out
    assert "Namespace:  default" in out
    assert "Kind:       Deployment" in out
    assert "replicas: 3" in out


def test_compact_truncation_marks_explicitly():
    """Truncation must be visible to an LLM, not a silent trailing '...'
    (which YAML also uses as a document-end marker and could be mistaken
    for the natural end of the value)."""
    big = {"x": "y" * 500}
    out = _compact(big, max_len=50)
    assert "[TRUNCATED" in out
    assert "full=" in out
    assert len(out) <= 50


def test_compact_short_value_untouched():
    v = {"x": "short"}
    out = _compact(v, max_len=200)
    assert "TRUNCATED" not in out


def test_compact_breaks_at_flow_separator():
    """When possible, cut at a comma/brace so the truncated output ends
    cleanly instead of slicing mid-token."""
    v = {"items": list(range(50))}  # flow-style: {items: [0, 1, 2, ...]}
    out = _compact(v, max_len=40)
    assert "[TRUNCATED" in out
    # Marker shouldn't be glued to a partial token; ends with the marker
    assert out.endswith("b]")
