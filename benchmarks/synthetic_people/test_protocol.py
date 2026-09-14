"""Checks for benchmark validity, not model quality."""

import importlib.util
from pathlib import Path

import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("synthetic_benchmark", HERE / "benchmark.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def test_frozen_dataset_and_condition_balance():
    m = b.setup()
    b.validate_inputs(m)
    assert len(m["cases"]) == 140
    assert len({c["id"] for c in m["cases"]}) == 140
    assert {g["character_index"] for g in m["gallery"]}.isdisjoint(m["unknown_character_indices"])
    for condition in {c["condition"] for c in m["cases"]}:
        cases = [c for c in m["cases"] if c["condition"] == condition]
        assert len(cases) == 20
        assert sum(c["expected"] == "UNKNOWN" for c in cases) == 5
        assert len({c["character_index"] for c in cases}) == 20


def test_prompt_inputs_only_contain_gallery_and_one_query():
    m = b.setup()
    case = next(c for c in m["cases"] if c["expected"] == "UNKNOWN")
    inputs = b.ordered_inputs(m, case)
    assert sum(k == "image" for k, _ in inputs) == 16
    text = "\n".join(v for k, v in inputs if k == "text")
    assert case["path"] not in text
    assert case["id"] not in text
    assert "expected" not in text
    assert all(text.count(g["label"]) == 1 for g in m["gallery"])
    assert inputs[-1] == ("image", b.core.image_b64(case))
    assert set(case["gallery_order"]) == set(range(15))


def test_truth_and_model_order_do_not_change_bytes():
    m = b.setup()
    c = m["cases"][0]
    mutated = {**c, "expected": "INJECTED_LABEL", "id": "INJECTED_ID", "character_index": 999}
    assert b.ordered_inputs(m, c) == b.ordered_inputs(m, mutated)


def test_faces_are_small_and_not_upscaled_from_source():
    m = b.setup()
    for c in m["cases"]:
        im = Image.open(HERE / c["path"])
        assert list(im.size) == c["image_size"]
        if c["condition"].startswith("face_"):
            size = int(c["condition"].split("_")[1])
            assert im.size == (size, size)
            assert min(c["processing"]["native_crop_size"]) >= size


def test_unknown_rejection_abstention_and_errors_are_distinct():
    b.setup()

    def row(expected, label, status="ok"):
        return {"expected": expected, "label": label, "status": status, "latency_s": 1}

    rows = [
        row("UNKNOWN", "UNKNOWN"),
        row("UNKNOWN", "UNCERTAIN"),
        row("UNKNOWN", b.core.LABELS[0]),
        row("UNKNOWN", None, "error"),
        row(b.core.LABELS[0], "UNCERTAIN"),
    ]
    result = b.core.metrics(rows)
    assert result["correct"] == 1
    assert result["unknown_correct"] == 1
    assert result["unknown_false_accepts"] == 1
    assert result["abstentions"] == 2
    assert result["errors"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        '{"label":"someone","confidence":0.5}',
        '{"label":"UNKNOWN","confidence":true}',
        '{"label":"UNKNOWN","confidence":1.1}',
        '{"label":"UNKNOWN","confidence":NaN}',
    ],
)
def test_invalid_output_is_not_scored_as_a_match(raw):
    b.setup()
    with pytest.raises(ValueError):
        b.core.parse_prediction(raw)
