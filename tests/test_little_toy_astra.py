"""Astra's detector contract, nearest selection, and failure boundaries."""
import base64
import json
from unittest.mock import Mock, patch

import cv2
import numpy as np
import pytest

from innate.exceptions import SkillCancelled, SkillFailed
from innate_skills import pick_up_pile_little_toy_astra as m
from test_little_toy_pickup import detection, payload, frame


def host():
    h = object.__new__(m.PickUpPileLittleToyAstra)
    h.mobility = Mock(); h.check_cancelled = Mock(); h.logger = Mock(); h.llm = Mock()
    h._box_exclusion = Mock(); h._box_exclusion.filter.side_effect = lambda c, image: c
    h._last_seen = None
    return h


def test_pins_responses_astra_ultrafast_and_strict_detector_format():
    llm = m.PickUpPileLittleToyAstra.llm
    assert llm.model == 'openai:gpt-6-astra'
    extra = json.loads(llm._extra_body)
    assert extra['service_tier'] == 'ultrafast'
    assert extra['text']['format']['strict'] is True
    assert extra['text']['format']['schema'] == m.DETECTION_SCHEMA
    assert m.PickUpPileLittleToyAstra.requires_llm


def test_same_yolo_native_pixel_response_and_tracker_tuple():
    p = payload([detection([600,400,800,600],[700,500])],1280,720)
    c, returned = m.parse_candidates(json.dumps(p),1280,720)
    assert returned == p
    assert c[0][0] == 350
    assert c[0][1] == pytest.approx(500*480/720)
    assert c[0][2] is None
    assert c[0][3] == pytest.approx((300,400*480/720,400,400))


@pytest.mark.parametrize('text', ['not json', '{}', json.dumps(payload([],640,480)),
    json.dumps(payload([detection([600,400,800,600],[900,500])],1280,720))])
def test_invalid_responses_rejected(text):
    with pytest.raises((ValueError, KeyError)):
        m.parse_candidates(text,1280,720)


def test_real_pipeline_sorts_nearest_not_confidence_and_scales_reference():
    h=host()
    h.llm.ask.return_value=json.dumps(payload([
        detection([100,300,200,400],[150,350],.99),
        detection([500,500,600,600],[550,550],.7),
        detection([10,10,20,20],[15,15],.9)],1280,720))
    def floor(u,v,tilt):return None if v<100 else (2,0) if u<100 else (.4,.1)
    with patch.object(m,'settled_frame',return_value=frame()),patch.object(m,'pixel_to_floor',side_effect=floor):
        c,image=h._detect_candidates('toy')
    assert len(c)==2 and c[0][0]==275
    assert cv2.imdecode(np.frombuffer(base64.b64decode(image),np.uint8),1).shape[:2]==(480,640)
    assert h.llm.ask.call_args.kwargs['retries']==1
    assert 'not 0-1000' in h.llm.ask.call_args.args[1]
    h._box_exclusion.filter.assert_called_once()


def test_empty_scene_returns_no_candidate():
    h=host();h.llm.ask.return_value=json.dumps(payload([],1280,720))
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
