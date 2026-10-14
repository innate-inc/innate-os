"""Toy selection, crop geometry and cancellation without hardware motion."""
import base64
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workspace'))
from innate.exceptions import SkillCancelled, SkillFailed
from innate_skills import pick_up_pile_little_toy as m


def detection(box, point, confidence=.8):
    return {'confidence': confidence, 'bbox_xyxy': box, 'grasp_point_xy': point}


def payload(detections, width=200, height=120):
    return {'image': {'width': width, 'height': height}, 'detections': detections}


def test_crop_clamps_all_edges():
    assert m.crop_window((0, 0, 20, 10), 100, 80) == (0, 0, 30, 15)
    assert m.crop_window((80, 70, 100, 80), 100, 80) == (70, 65, 100, 80)


def test_reject_aggregate_outside_and_missing_mask_preserve_confidence_order():
    p=payload([detection([50,30,150,90],[100,60],.99),
               detection([100,70,130,85],[115,80],.8),
               detection([0,0,20,20],[10,10],.9),
               detection([60,40,70,50],None,.9),
               detection([60,40,70,50],[65,45],.6)])
    result=m.individual_candidates(p,(600,350,800,470),(650,380,750,440),(1280,720))
    assert len(result)==2
    assert result[0][0]==pytest.approx(715/2)
    assert result[0][1]==pytest.approx(430*480/720)
    assert result[0][3]==pytest.approx((350,420*480/720,365,435*480/720))


def test_pile_only_returns_no_grasp_point():
    p=payload([detection([50,30,150,90],[100,60])])
    assert m.individual_candidates(p,(600,350,800,470),(650,380,750,440),(1280,720))==[]


def test_bad_crop_dimensions_and_invalid_grasp_rejected():
    with pytest.raises(ValueError,match='dimensions'):
        m.individual_candidates(payload([],width=199),(600,350,800,470),(650,380,750,440),(1280,720))
    with pytest.raises(ValueError,match='outside'):
        m.individual_candidates(payload([detection([50,30,60,40],[80,80])]),(600,350,800,470),(650,380,750,440),(1280,720))


def host():
    h=object.__new__(m.PickUpPileLittleToy)
    h.mobility=Mock();h.check_cancelled=Mock();h.logger=Mock()
    h._box_exclusion=Mock();h._box_exclusion.filter.side_effect=lambda c,image:c
    return h


def frame():
    return base64.b64encode(m.encode_jpeg(np.zeros((720,1280,3),np.uint8))).decode()


def test_real_detection_pipeline_returns_controller_sized_reference():
    h=host();pile=payload([detection([640,400,740,460],[690,430])],1280,720)
    toy=payload([detection([110,75,140,95],[125,85])],200,120)
    with patch.object(m,'settled_frame',return_value=frame()),patch.object(m,'request_yoloe',side_effect=[pile,toy]) as request:
        c,image=h._detect_candidates('toy')
    assert len(c)==1 and request.call_count==2
    decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image),np.uint8),cv2.IMREAD_COLOR)
    assert decoded.shape[:2]==(480,640)
    assert c[0][:2]==pytest.approx((357.5,455*480/720))
    h._box_exclusion.filter.assert_called_once()


def test_empty_pile_skips_second_request():
    h=host()
    with patch.object(m,'settled_frame',return_value=frame()),patch.object(m,'request_yoloe',return_value=payload([],1280,720)) as request:
        assert h._detect_candidates('toy')[0]==[]
    request.assert_called_once()


def test_cancel_after_pile_request_skips_crop():
    h=host();h.check_cancelled.side_effect=[None,SkillCancelled('stop')]
    with patch.object(m,'settled_frame',return_value=frame()),patch.object(m,'request_yoloe',return_value=payload([],1280,720)) as request:
        with pytest.raises(SkillCancelled):h._detect_candidates('toy')
    request.assert_called_once()


def test_detector_failure_does_not_fallback_to_pile_center():
    h=host()
    with patch.object(m,'settled_frame',return_value=frame()),patch.object(m,'request_yoloe',side_effect=OSError('offline')):
        with pytest.raises(SkillFailed,match='offline'):h._detect_candidates('toy')


def test_redetection_preserves_existing_instance_gate():
    h=host();h._last_seen=(1.,0.,1.);h._coasts=0
    c=[(10,20,None,(0,0,30,30)),(30,40,None,(20,20,50,50))]
    h._memory_dist=lambda candidate: .01 if candidate is c[1] else .5
    with patch.object(m,'pixel_to_floor',return_value=(1,0)):
        assert h._choose_cand(c) is c[1]
    h._memory_dist=lambda candidate: 1.
    with patch.object(m,'pixel_to_floor',return_value=(1,0)):
        assert h._choose_cand(c) is None
