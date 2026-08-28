import sys
from pathlib import Path
from types import SimpleNamespace

from nav_msgs.msg import OccupancyGrid
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.skills.robot_state import RobotStateProvider
from brain_client.state.pose import Pose

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "workspace"))

from innate_skills.move_straight import _crosses_keepout  # noqa: E402


class _FeedNode:
    def __init__(self):
        self.subscriptions = {}

    def create_subscription(self, _message_type, topic, callback, qos):
        self.subscriptions[topic] = (callback, qos)
        return object()


def _grid(values, *, load_time=1):
    msg = OccupancyGrid()
    msg.header.frame_id = "map"
    msg.info.width = 2
    msg.info.height = 2
    msg.info.resolution = 0.1
    msg.info.map_load_time.sec = load_time
    msg.data = values
    return msg


def test_keepout_subscription_requests_latched_state():
    state_node = SimpleNamespace(get_logger=lambda: SimpleNamespace())
    feed_node = _FeedNode()
    manipulation = SimpleNamespace(node=feed_node, start=lambda: None, stop=lambda: None)
    provider = RobotStateProvider(
        state_node,
        None,
        manipulation=manipulation,
        mobility=None,
        head=None,
        memory=None,
        head_current_position_topic="/head/current_position",
    )

    provider.start_subscriptions()

    _callback, qos = feed_node.subscriptions["/nav/keepout_filter_mask"]
    assert isinstance(qos, QoSProfile)
    assert qos.reliability == QoSReliabilityPolicy.RELIABLE
    assert qos.history == QoSHistoryPolicy.KEEP_LAST
    assert qos.depth == 1
    assert qos.durability == QoSDurabilityPolicy.TRANSIENT_LOCAL


def test_skill_map_keeps_grid_raw_and_exposes_keepout_cells():
    provider = object.__new__(RobotStateProvider)
    provider.last_map = _grid([0, 0, -1, 100])
    provider.last_keepout_map = _grid([0, 100, 0, 0])
    provider._map_cache = None

    # grid stays the raw /map: memory coverage reads it as physical occupancy.
    assert provider.current_map().grid.tolist() == [[0, 0], [-1, 100]]
    assert provider.current_map().keepout_grid.tolist() == [[0, 100], [0, 0]]


def test_skill_map_ignores_retained_mask_from_another_map_load():
    provider = object.__new__(RobotStateProvider)
    provider.last_map = _grid([0, 0, 0, 0], load_time=2)
    provider.last_keepout_map = _grid([100, 100, 100, 100], load_time=1)
    provider._map_cache = None

    assert provider.current_map().grid.tolist() == [[0, 0], [0, 0]]
    assert provider.current_map().keepout_grid is None


def test_direct_motion_checks_its_footprint_against_keepouts():
    base = OccupancyGrid()
    base.header.frame_id = "map"
    base.info.width = base.info.height = 20
    base.info.resolution = 0.1
    base.info.origin.position.x = base.info.origin.position.y = -1.0
    base.info.map_load_time.sec = 1
    base.data = [0] * 400
    keepout = OccupancyGrid()
    keepout.header = base.header
    keepout.info = base.info
    keepout.data = [0] * 400
    keepout.data[10 * 20 + 14] = 100

    provider = object.__new__(RobotStateProvider)
    provider.last_map = base
    provider.last_keepout_map = keepout
    provider._map_cache = None
    pose = Pose(x=0, y=0, theta=0)

    assert _crosses_keepout(provider.current_map(), pose, 0.5)
    assert not _crosses_keepout(provider.current_map(), pose, -0.5)
