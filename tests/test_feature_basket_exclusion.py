import sys,time,json,tempfile,math
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workspace'))
from innate_skills import feature_basket_exclusion as m
from innate_skills import box_features as bf
from innate.exceptions import SkillFailed

class Tests(unittest.TestCase):
 def host(self):
  return NS(odom=NS(x=0.,y=0.,theta=0.,stamp=time.time(),frame_id='odom'),logger=Mock(),check_cancelled=Mock(),_p={'tilt_deg':-35})
 def test_store_recall_expands_physical_footprint(self):
  with tempfile.TemporaryDirectory() as d,patch.object(bf,'candidate_face_in_base',return_value=(np.array([1.,0]),np.array([1.,0]))),patch.object(m,'CACHE',Path(d)/'cache'):
   h=self.host();v=m.remember(h,{'best':{'face_width_m':.5,'reference':'long'}},-35)
   np.testing.assert_allclose(v.min(axis=0),[.9,-.35,0])
   np.testing.assert_allclose(v.max(axis=0),[1.43,.35,.25])
   np.testing.assert_allclose(m.recall(),v)
   data=json.loads(m.CACHE.read_text());data['boot']='other';m.CACHE.write_text(json.dumps(data));self.assertIsNone(m.recall())
 def test_short_face_uses_other_dimensions(self):
  with tempfile.TemporaryDirectory() as d,patch.object(bf,'candidate_face_in_base',return_value=(np.array([1.,0]),np.array([1.,0]))),patch.object(m,'CACHE',Path(d)/'cache'):
   v=m.remember(self.host(),{'best':{'face_width_m':.33}},-35)
   self.assertAlmostEqual(v[:,0].max(),1.6);self.assertAlmostEqual(v[:,1].max(),.265)
 def exclusion(self):
  e=object.__new__(m.FeatureBasketExclusion);e.host=self.host();e.world_vertices=None;e.observer=Mock();e.reuse_drop_pose=False;return e
 def test_cached_pose_avoids_request(self):
  e=self.exclusion();e.reuse_drop_pose=True;e.refresh('anything');e.observer.capture.assert_not_called()
 def test_missing_detection_preserves_world_pose(self):
  e=self.exclusion();e.world_vertices=np.zeros((8,3));e.observer.capture.return_value={'detected':False}
  original=e.world_vertices;e.refresh('aW1hZ2U=');self.assertIs(e.world_vertices,original)
 def test_api_failure_without_memory_stops(self):
  e=self.exclusion();e.observer.capture.side_effect=ValueError('offline')
  with self.assertRaisesRegex(SkillFailed,'no remembered'):e.refresh('aW1hZ2U=')
 def test_api_failure_with_memory_keeps_exclusion(self):
  e=self.exclusion();e.world_vertices=np.zeros((8,3));e.observer.capture.side_effect=ValueError('offline');e.refresh('aW1hZ2U=')
 def test_moved_during_request_rejected(self):
  e=self.exclusion()
  def capture(_):e.host.odom.x=.1;return {'detected':False}
  e.observer.capture.side_effect=capture
  with self.assertRaisesRegex(SkillFailed,'moved'):e.refresh('aW1hZ2U=')
 def test_filter_bbox_overlap_and_floor_margin_preserves_order(self):
  e=self.exclusion();e.refresh=Mock();e.vertices=Mock(return_value=np.array([[x,y,z] for x in (1.,2.) for y in (-.5,.5) for z in (0.,.25)]))
  hull=np.array([[[100,0]],[[200,0]],[[200,200]],[[100,200]]],dtype=np.float32)
  candidates=[(90,50,None,(80,40,110,60)),(300,300,None,(290,290,310,310)),(400,300,None,(390,290,410,310)),(450,300,None,(440,290,460,310))]
  with patch.object(m,'projected_hull',return_value=hull),patch.object(m,'pixel_to_floor',side_effect=[(3,0),(1.5,0),(3,1),(3,2)]):
   self.assertEqual(e.filter(candidates,'image'),candidates[2:])
 def test_unknown_basket_does_not_select_unprotected_sock(self):
  e=self.exclusion();e.refresh=Mock()
  self.assertEqual(e.filter([(300,300,None,(290,290,310,310))],'image'),[])
 def test_transform_after_turn_keeps_same_world_basket(self):
  e=self.exclusion();e.world_vertices=np.array([[1.,0.,0.]]*8);e.host.odom.theta=math.pi/2
  np.testing.assert_allclose(e.vertices()[0],[0,-1,0],atol=1e-9)
 def test_stale_odometry(self):
  h=self.host();h.odom.stamp-=1
  with self.assertRaises(SkillFailed):m.odom_xy(h)

class SkillTests(unittest.TestCase):
 def test_separate_skill_cleanup_on_cancel(self):
  from innate_skills import pick_sock_pile_yoloe_features as skill
  from innate.exceptions import SkillCancelled
  h=NS()
  with patch.object(skill,'FeatureBasketExclusion') as exclusion,patch.object(skill.PickSockYoloe,'execute',side_effect=SkillCancelled('stop')):
   with self.assertRaises(SkillCancelled):skill.PickSockPileYoloeFeatures.execute(h)
   exclusion.return_value.close.assert_called_once()
 def test_empty_filter_result_returns_to_existing_search(self):
  from innate_skills.pick_sock_pile_yoloe_features import PickSockPileYoloeFeatures
  from innate_skills.pick_sock_yoloe import PickSockYoloe
  h=object.__new__(PickSockPileYoloeFeatures)
  # Bypass hardware construction; the candidate pipeline is the real inherited path.
  h._box_exclusion=Mock();h._box_exclusion.filter.return_value=[]
  with patch.object(PickSockYoloe,'_detect_candidates',return_value=([(1,2,None,(0,0,3,4))],'image')),patch.object(PickSockPileYoloeFeatures,'logger',new_callable=lambda:Mock(),create=True):
   self.assertEqual(h._detect_candidates('pile'),([], 'image'))

if __name__=='__main__':unittest.main()
