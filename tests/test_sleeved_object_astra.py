"""Astra's detector contract, nearest selection, and failure boundaries."""
import base64
import json
from unittest.mock import Mock, patch

import cv2
import numpy as np
import pytest

from innate.exceptions import SkillCancelled, SkillFailed
from innate_skills import pick_up_sleeved_object_astra as m
from test_little_toy_pickup import frame


def host():
    h = object.__new__(m.PickUpSleevedObjectAstra)
    h.mobility = Mock(); h.check_cancelled = Mock(); h.logger = Mock(); h.llm = Mock()
    h._box_exclusion = Mock(); h._box_exclusion.filter.side_effect = lambda c, image: c
    h._last_seen = None
    return h


def test_pins_responses_astra_ultrafast_and_strict_detector_format():
    llm = m.PickUpSleevedObjectAstra.llm
    assert llm.model == 'openai:gpt-6-astra'
    extra = json.loads(llm._extra_body)
    assert extra['service_tier'] == 'ultrafast'
    assert extra['text']['format']['strict'] is True
    assert extra['text']['format']['schema'] == m.DETECTION_SCHEMA
    assert m.PickUpSleevedObjectAstra.requires_llm


def test_same_yolo_native_pixel_response_and_tracker_tuple():
    c, returned = m.parse_candidates('{"box":[600,400,800,600]}',1280,720)
    assert returned['image'] == {'width':1280,'height':720}
    assert returned['detections'][0]['grasp_point_xy'] == [700,500]
    assert c[0][0] == 350
    assert c[0][1] == pytest.approx(500*480/720)
    assert c[0][2] is None
    assert c[0][3] == pytest.approx((300,400*480/720,400,400))


@pytest.mark.parametrize('text', ['not json', '{}', '[]',
    '{"box":[1,2,3]}', '{"box":[0,0,1300,500]}', '{"box":[5,5,4,4]}',
    '{"box":[0,0,NaN,4]}', '{"box":[true,0,4,4]}',
    '{"box":[[1,2,3,4],[5,6,7,8]]}', '{"box":null,"extra":1}'])
def test_invalid_responses_rejected(text):
    with pytest.raises((ValueError, KeyError)):
        m.parse_candidates(text,1280,720)


def test_real_pipeline_single_box_and_scales_reference():
    h=host()
    h.llm.ask.return_value='{"box":[500,500,600,600]}'
    with patch.object(m,'settled_frame',return_value=frame()),patch.object(m,'pixel_to_floor',return_value=(.4,.1)):
        c,image=h._detect_candidates('toy')
    assert len(c)==1 and c[0][0]==275
    assert cv2.imdecode(np.frombuffer(base64.b64decode(image),np.uint8),1).shape[:2]==(480,640)
    assert h.llm.ask.call_args.kwargs['retries']==1
    question = h.llm.ask.call_args.args[1]
    assert len(question) < 240
    assert 'protective sleeve' in question and 'floor ahead' in question
    assert 'Exclude unsleeved objects' in question
    assert 'protective sleeve' in m.PickUpSleevedObjectAstra.detection_prompt
    h._box_exclusion.filter.assert_called_once()


def test_unprojectable_box_does_not_become_target():
    h=host();h.llm.ask.return_value='{"box":[0,0,10,10]}'
    with patch.object(m,'settled_frame',return_value=frame()),patch.object(m,'pixel_to_floor',return_value=None):
        assert h._detect_candidates('toy')[0]==[]


def test_empty_scene_returns_no_candidate():
    h=host();h.llm.ask.return_value='{"box":null}'
    with patch.object(m,'settled_frame',return_value=frame()):
        assert h._detect_candidates('toy')[0]==[]


@pytest.mark.parametrize('camera,response',[(None,'{}'),('bad','{}'),(frame(),None),(frame(),'not json')])
def test_missing_camera_or_bad_model_response_fails_without_target(camera,response):
    h=host();h.llm.ask.return_value=response
    with patch.object(m,'settled_frame',return_value=camera):
        with pytest.raises(SkillFailed):h._detect_candidates('toy')


def test_cancel_after_model_never_returns_target():
    h=host();h.check_cancelled.side_effect=[None,SkillCancelled('stop')]
    with patch.object(m,'settled_frame',return_value=frame()):
        with pytest.raises(SkillCancelled):h._detect_candidates('toy')
    h._box_exclusion.filter.assert_not_called()
