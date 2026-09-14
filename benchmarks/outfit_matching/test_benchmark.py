import base64
import json

import benchmark
import pytest


def test_unknown_rejection_is_not_abstention_or_service_error():
    rows = [
        {"expected": "UNKNOWN", "label": "UNKNOWN", "status": "ok", "latency_s": 1},
        {"expected": "UNKNOWN", "label": "UNCERTAIN", "status": "ok", "latency_s": 2},
        {"expected": "UNKNOWN", "label": "OUTFIT_01", "status": "ok", "latency_s": 3},
        {"expected": "UNKNOWN", "label": None, "status": "error", "latency_s": 120},
        {"expected": "OUTFIT_01", "label": "OUTFIT_01", "status": "ok", "latency_s": 5},
        {"expected": "OUTFIT_02", "label": "OUTFIT_01", "status": "ok", "latency_s": 6},
    ]
    result = benchmark.metrics(rows)
    assert result["correct"] == 2
    assert result["unknown_n"] == 4
    assert result["unknown_correct"] == 1
    assert result["unknown_false_accepts"] == 1
    assert result["known_correct"] == 1
    assert result["abstentions"] == 1
    assert result["errors"] == 1
    assert result["median_latency_s"] == 3
    assert result["p95_latency_s"] == 6


@pytest.mark.parametrize(
    "prediction",
    [
        {"label": "OUTFIT_99", "confidence": 0.8},
        {"label": "UNKNOWN", "confidence": True},
        {"label": "UNKNOWN", "confidence": float("nan")},
        {"label": "UNKNOWN", "confidence": 1.1},
        {"label": "UNKNOWN"},
        {"label": "UNKNOWN", "confidence": 0.8, "extra": "ignore me"},
        ["UNKNOWN", 0.8],
    ],
)
def test_malformed_output_is_an_error(prediction):
    with pytest.raises(ValueError):
        benchmark.parse_prediction(json.dumps(prediction))


def test_requests_do_not_leak_query_truth_or_unknown_references(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "HERE", tmp_path)
    gallery = []
    for i in range(15):
        path = tmp_path / f"{i}.jpg"
        path.write_bytes(f"ref{i}".encode())
        gallery.append({"path": path.name, "label": benchmark.LABELS[i]})
    (tmp_path / "SECRET_UNKNOWN.jpg").write_bytes(b"query")
    case = {
        "path": "SECRET_UNKNOWN.jpg",
        "expected": "SECRET_TRUTH",
        "outfit_index": 18,
        "gallery_order": list(reversed(range(15))),
    }
    items = benchmark.ordered_inputs({"gallery": gallery}, case)
    assert len([i for i in items if i[0] == "image"]) == 16
    text = " ".join(value for kind, value in items if kind == "text")
    assert "SECRET_UNKNOWN" not in text
    assert "SECRET_TRUTH" not in text
    assert items[1] == ("text", "Reference outfit OUTFIT_15")
    assert base64.b64decode(items[-1][1]) == b"query"


def test_empty_metrics_does_not_imply_perfect_accuracy():
    result = benchmark.metrics([])
    assert result["n"] == 0
    assert result["median_latency_s"] is None
