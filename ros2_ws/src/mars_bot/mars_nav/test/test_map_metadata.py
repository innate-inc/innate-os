# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from mars_nav.map_metadata import normalize_legacy_trinary_metadata, repair_legacy_trinary_map


def test_repairs_humble_trinary_threshold_without_reformatting(tmp_path):
    original = """image: OfficeClean.pgm
mode: trinary
resolution: 0.05
free_thresh: 0.25  # Nav2 Humble default
occupied_thresh: 0.65
"""
    map_yaml = tmp_path / "OfficeClean.yaml"
    map_yaml.write_text(original)

    changed = repair_legacy_trinary_map(map_yaml)

    assert changed is True
    assert map_yaml.read_text() == original.replace("free_thresh: 0.25", "free_thresh: 0.196")


def test_leaves_safe_or_non_trinary_maps_unchanged():
    safe = "mode: trinary\nfree_thresh: 0.196\n"
    scale = "mode: scale\nfree_thresh: 0.25\n"

    assert normalize_legacy_trinary_metadata(safe) == (safe, False)
    assert normalize_legacy_trinary_metadata(scale) == (scale, False)
