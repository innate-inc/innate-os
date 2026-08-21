from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "ros2_ws/src/mars_bot/mars_nav/config/costmap.yaml"


def test_navigation_costmaps_use_the_separate_keepout_filter():
    text = CONFIG.read_text()
    assert text.count('filters: ["keepout_filter", "keepout_inflation"]') == 2
    assert text.count('plugin: "nav2_costmap_2d::KeepoutFilter"') == 2
    assert text.count('filter_info_topic: "/nav/keepout_filter_info"') == 2


def test_localization_map_topic_is_unchanged():
    assert 'map_topic: "/map"' in CONFIG.read_text()
