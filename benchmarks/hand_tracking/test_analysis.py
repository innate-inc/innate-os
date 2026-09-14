import pytest
from analyze import direction_check, envelope, iou, read_frozen, score_samples


def hand_box(x=20):
    return {"xy": [[x, 20]] * 20 + [[x + 20, 40]]}


def row(frame, hands):
    return {"clip_id": "clip", "frame": frame, "pts_ms": frame * 100, "width": 100, "height": 100, "hands": hands}


def label(frame, present=True, ambiguous=False):
    return {
        "clip_id": "clip",
        "frame": frame,
        "pts_ms": frame * 100,
        "condition": "hold",
        "present": present,
        "identity_ambiguous": ambiguous,
        "bbox_xyxy": [20, 20, 40, 40] if present else None,
    }


def test_presence_is_distinct_from_correct_target_localization():
    result = score_samples(
        [row(0, [hand_box(70)]), row(1, []), row(2, [hand_box()]), row(3, [])],
        [label(0), label(1), label(2, False), label(3, False)],
    )
    assert result["sampled_presence"] == {"tp": 1, "fn": 1, "tn": 1, "fp": 1}
    assert result["target_localization"]["denominator"] == 2
    assert result["target_localization"]["iou_ge_0_3"] == 0


def test_missing_prediction_is_an_error_not_a_dropped_denominator():
    with pytest.raises(ValueError, match="Missing annotated"):
        score_samples([], [label(0)])
    with pytest.raises(ValueError, match="Duplicate"):
        score_samples([row(0, []), row(0, [])], [label(0)])


def test_clipped_and_ambiguous_samples():
    hand = {"xy": [[-10, -10]] * 20 + [[120, 120]]}
    assert envelope(hand, 100, 100) == [0, 0, 100, 100]
    assert iou(envelope(hand, 100, 100), [0, 0, 100, 100]) == 1
    assert iou([0, 0, 0, 0], [0, 0, 0, 0]) == 0
    result = score_samples([row(0, [hand_box()])], [label(0, ambiguous=True)])
    assert result["sampled_presence"]["tp"] == 1
    assert result["target_localization"]["denominator"] == 0


def test_direction_abstains_on_missing_or_stationary_data():
    windows = [[0, 400], [500, 900]]
    assert direction_check([row(i, []) for i in range(10)], "x", 1, windows)["status"] == "abstain"
    assert direction_check([row(i, [hand_box()]) for i in range(10)], "x", 1, windows)["status"] == "abstain"
    moving = [row(i, [hand_box(i * 5)]) for i in range(10)]
    assert direction_check(moving, "x", 1, windows)["status"] == "correct"
    assert direction_check(moving, "x", -1, windows)["status"] == "wrong"


def test_reference_tampering_fails(tmp_path):
    path = tmp_path / "annotations.json"
    path.write_text("{}")
    path.with_suffix(".sha256").write_text("incorrect")
    with pytest.raises(ValueError, match="Reference changed"):
        read_frozen(path)
