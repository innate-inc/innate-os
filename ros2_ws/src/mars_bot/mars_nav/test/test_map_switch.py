"""The map identity must not advance while the previous AMCL filter is alive."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from lifecycle_msgs.msg import State

from mars_nav import mode_manager as module


@pytest.mark.parametrize("reset_ok, restore_ok", [(True, True), (True, False), (False, True), (False, False)])
def test_reset_amcl_before_announcing_new_map(monkeypatch, reset_ok, restore_ok):
    manager = SimpleNamespace(
        current_map="A.yaml", _service_clients={}, get_logger=Mock(), _load_map_on_server=Mock(return_value=True)
    )
    calls = []

    def transition(clients, logger, node, state, **kwargs):
        calls.append((node, state, manager.current_map))
        if state == State.PRIMARY_STATE_UNCONFIGURED:
            return reset_ok
        return restore_ok if node == "navigation_amcl" and state == State.PRIMARY_STATE_ACTIVE else True

    monkeypatch.setattr(module, "transition_node", transition)
    success, message = module.ModeManager._efficient_map_switch(manager, "B.yaml")
    assert success == (reset_ok and restore_ok)
    assert ("navigation_amcl", State.PRIMARY_STATE_UNCONFIGURED, "A.yaml") in calls
    if reset_ok:
        assert ("navigation_amcl", State.PRIMARY_STATE_ACTIVE, "B.yaml") in calls
        manager._load_map_on_server.assert_called_once()
    else:
        assert manager.current_map == "A.yaml"
        manager._load_map_on_server.assert_not_called()
        for node in ("navigation_amcl", "bt_navigator"):
            assert (node, State.PRIMARY_STATE_ACTIVE, "A.yaml") in calls
        assert "navigation_amcl_reset" in message
    if not restore_ok:
        assert "'navigation_amcl'" in message
