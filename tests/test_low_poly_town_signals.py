from sim.tools.low_poly_town_signals import isolate_signal_materials


def _fixture_obj() -> str:
    lines = ["mtllib town.mtl", "o Decorative_Bush", "usemtl Red", "f 1 2 3"]
    ns = (
        "Traffic_Light.001_Cube.009",
        "Traffic_Light.002_Cube.010",
        "Traffic_Light.003_Cube.011",
        "Traffic_Light_Cube.008",
        "Traffic_Light.009_Cube.018",
        "Traffic_Light.011_Cube.024",
    )
    ew = (
        "Traffic_Light.004_Cube.012",
        "Traffic_Light.005_Cube.013",
        "Traffic_Light.006_Cube.014",
        "Traffic_Light.007_Cube.019",
        "Traffic_Light.008_Cube.016",
        "Traffic_Light.010_Cube.020",
    )
    for name in (*ns, *ew):
        lines.append(f"o {name}")
        for aspect in ("Red", "Yellow", "Green"):
            lines.extend((f"usemtl {aspect}", "f 1 2 3"))
    return "\n".join(lines) + "\n"


def test_signal_materials_are_phase_isolated_without_recoloring_decorations():
    mtl = "newmtl Red\nKd 1 0 0\nnewmtl Yellow\nKd 1 1 0\nnewmtl Green\nKd 0 1 0\n"
    obj, rewritten_mtl = isolate_signal_materials(_fixture_obj(), mtl)

    assert "o Decorative_Bush\nusemtl Red" in obj
    assert obj.count("usemtl Signal_NS_Red") == 6
    assert obj.count("usemtl Signal_EW_Green") == 6
    assert "newmtl Signal_NS_Yellow\nKd 1 1 0" in rewritten_mtl
    assert "newmtl Signal_EW_Green\nKd 0 1 0" in rewritten_mtl


def test_signal_material_rewrite_fails_closed_when_a_head_is_missing():
    mtl = "newmtl Red\nKd 1 0 0\nnewmtl Yellow\nKd 1 1 0\nnewmtl Green\nKd 0 1 0\n"
    obj = _fixture_obj().replace("usemtl Green", "usemtl Gray", 1)

    try:
        isolate_signal_materials(obj, mtl)
    except RuntimeError as exc:
        assert "unexpected traffic-light material assignments" in str(exc)
    else:
        raise AssertionError("missing signal lens must reject the generated environment")
