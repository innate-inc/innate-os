#!/usr/bin/env python3
"""Evaluate the pinned PR face path on the frozen synthetic-character set only."""
from __future__ import annotations
import argparse
import json
import platform
import sys
import time
from collections import Counter
from pathlib import Path
import cv2
import numpy as np
import benchmark
from benchmark import HERE, core
sys.path.insert(0,str(HERE/'pr807'))
from extracted_face import FaceLocator, FaceEmbedder, PersonDetector, crop, _best

ACCEPT=0.42
MARGIN=0.05
MIN_FACE_PX=24


def locate_embed(image,locator,embedder):
 hits=locator.locate(image)
 eligible=[h for h in hits if h.h>=MIN_FACE_PX]
 if not eligible:
  return None, {'reason':'no_face' if not hits else 'below_24px',
   'detected_face_heights':[h.h for h in hits], 'eligible_face_count':0}
 hit=max(eligible,key=lambda h:h.score)
 vector=embedder.embed(image,hit)
 return vector,{'reason':'embedded' if vector is not None else 'embed_failed',
  'detected_face_heights':[h.h for h in hits], 'eligible_face_count':len(eligible),
  'chosen_face_height':hit.h,'detector_score':hit.score}


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--mode',choices=['face','hog-face'],default='face')
 args=parser.parse_args()
 manifest=benchmark.setup()
 benchmark.validate_inputs(manifest)
 source=json.loads((HERE/'pr807/source.json').read_text())
 assert core.digest(HERE/'pr807/extracted_face.py')==source['extracted_sha256']
 for m in json.loads((HERE/'pr807/model_provenance.json').read_text()):
  assert core.digest(HERE/'pr807'/m['path'])==m['sha256']
 cv2.setNumThreads(1)
 start=time.monotonic()
 locator=FaceLocator(HERE/'pr807/models/face_detection_yunet_2023mar.onnx')
 embedder=FaceEmbedder(HERE/'pr807/models/face_recognition_sface_2021dec.onnx')
 detector=PersonDetector() if args.mode=='hog-face' else None
 gallery=[];gallery_details=[]
 for item in manifest['gallery']:
  im=cv2.imread(str(HERE/item['path']))
  vector,diag=locate_embed(im,locator,embedder)
  if vector is None:raise RuntimeError(f"Gallery could not embed: {item['label']}")
  gallery.append((item['label'],vector))
  gallery_details.append({'label':item['label'],**diag})
 init=time.monotonic()-start
 for _ in range(3):locate_embed(im,locator,embedder)
 out=HERE/'results'/('pr807-'+args.mode)
 if out.exists():raise ValueError('Run already exists')
 out.mkdir(parents=True)
 config={'synthetic_only':True,'model':'YuNet 2023mar + SFace 2021dec',
  'mode':args.mode,'pr_commit':source['commit'],'manifest_sha256':core.digest(HERE/'manifest.json'),
  'runner_sha256':core.digest(Path(__file__)),'extract_sha256':source['extracted_sha256'],
  'face_accept':ACCEPT,'face_margin':MARGIN,'min_face_px':MIN_FACE_PX,'head_fraction':0.4 if detector else None,
  'yunet_score_threshold':0.6,'enrollment':'one vector per supplied reference, no updates',
  'opencv':cv2.__version__,'opencv_threads':cv2.getNumThreads(),'machine':platform.machine(),
  'platform':platform.platform(),'model_and_gallery_initialization_s':init,
  'warmup_iterations':3,'missing_face_protocol_label':'UNCERTAIN',
  'missing_face_note':'The PR cannot establish identity; this protocol records an abstention, not successful unknown rejection.',
  'gallery_details':gallery_details}
 core.dump(out/'config.json',config)
 cases=[c for c in manifest['cases'] if not detector or not c['condition'].startswith('face_')]
 for case in cases:
  result={k:v for k,v in case.items() if k!='gallery_order'}
  result.update(model=config['model'],provider='local',confidence=None,status='ok')
  start=time.monotonic()
  try:
   image=cv2.imread(str(HERE/case['path']))
   boxes=detector.detect(image) if detector else [(0.,0.,1.,1.)]
   candidates=[];diagnostics=[]
   for box in boxes:
    search=crop(image,(box[0],box[1],box[0]+(box[2]-box[0])*0.4,box[3])) if detector else image
    vector,diag=locate_embed(search,locator,embedder)
    diagnostics.append({'box':box,**diag})
    if vector is not None:
     ranked=sorted([(name,float(vec@vector)) for name,vec in gallery],key=lambda p:p[1],reverse=True)
     label=_best(gallery,vector,ACCEPT,MARGIN)
     candidates.append({'label':label,'best_label':ranked[0][0],'best_score':ranked[0][1],
      'runner_up_score':ranked[1][1],'margin':ranked[0][1]-ranked[1][1]})
   # Each scene has one target; avoid choosing an identity by consulting its label.
   if len(candidates)==1:
    result.update(candidates[0])
    result['label']=result['label'] or 'UNKNOWN'
    result['reason']='accepted' if result['label']!='UNKNOWN' else 'score_or_margin_reject'
   elif len(candidates)>1:
    result.update(label='UNCERTAIN',reason='multiple_face_candidates')
   else:
    result.update(label='UNCERTAIN',reason='no_person' if not boxes else ('below_24px' if any(d['reason']=='below_24px' for d in diagnostics) else 'no_usable_face'))
   result.update(person_box_count=len(boxes) if detector else None,diagnostics=diagnostics,
                 embedded_face_count=len(candidates),pr_identity_state='known' if result['label'] in core.LABELS else 'unknown')
  except (cv2.error,ValueError) as exc:
   result.update(status='error',label=None,reason=type(exc).__name__)
  result['latency_s']=round(time.monotonic()-start,6)
  with (out/'predictions.jsonl').open('a') as f:f.write(json.dumps(result)+'\n')
 core.summarize(out)
 print('Reasons:',dict(Counter(json.loads(line)['reason'] for line in (out/'predictions.jsonl').read_text().splitlines())))

if __name__=='__main__':main()
