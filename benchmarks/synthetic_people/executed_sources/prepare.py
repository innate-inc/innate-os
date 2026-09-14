#!/usr/bin/env python3
"""Package only the five generated, visually audited synthetic-character sheets."""
from __future__ import annotations
import hashlib
import json
import random
import sys
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'pr807'))
from extracted_face import FaceLocator

LAYOUTS = {
 'sheet_1': {'x':[0,365,729,1122], 'y':[0,280,560,840,1121,1402]},
 'sheet_2': {'x':[0,355,709,1122], 'y':[0,269,542,812,1088,1402]},
 'sheet_3': {'x':[0,367,744,1122], 'y':[0,280,560,840,1122,1402]},
 'sheet_4': {'x':[0,384,738,1122], 'y':[0,280,560,840,1122,1402]},
 'face_queries': {'x':[0,314,575,848,1122], 'y':[0,280,560,837,1114,1402]},
}
NAMES = dict(zip([1,2,3,5,6,7,8,10,11,12,13,15,16,17,18],
 ['Arin Vale','Nessa Reed','Corin Hale','Jalen Moss','Bren Alder',
  'Liora Finch','Mira Lark','Doran Pine','Sera Wren','Tavin Frost',
  'Naya Brook','Riven Ash','Kian Dune','Elin Cove','Zoren Birch'], strict=True))


def digest(p):
 return hashlib.sha256(p.read_bytes()).hexdigest()


def dump(p,data):
 p.parent.mkdir(parents=True,exist_ok=True)
 p.write_text(json.dumps(data,indent=2)+'\n')


def panel(name,row,col):
 p=HERE/'assets/sheets'/f'{name}.png'
 im=Image.open(p).convert('RGB')
 layout=LAYOUTS[name]
 assert im.size==(layout['x'][-1],layout['y'][-1])
 bounds=(layout['x'][col]+2,layout['y'][row]+2,layout['x'][col+1]-2,layout['y'][row+1]-2)
 return im.crop(bounds), {'source':str(p.relative_to(HERE)),'panel_bounds':bounds}


def face_crop(im,locator):
 hits=locator.locate(cv2.cvtColor(np.asarray(im),cv2.COLOR_RGB2BGR))
 if len(hits)!=1:
  raise ValueError(f'Expected exactly one face for preparation, found {len(hits)}')
 h=hits[0]
 # Square crop centered on the face box; no enhancement or alignment.
 side=max(h.w,h.h)*1.04
 cx,cy=h.x+h.w/2,h.y+h.h/2
 bounds=(round(cx-side/2),round(cy-side/2),round(cx+side/2),round(cy+side/2))
 if not(0<=bounds[0]<bounds[2]<=im.width and 0<=bounds[1]<bounds[3]<=im.height):
  raise ValueError(f'Face crop outside panel: {bounds}')
 return im.crop(bounds), {'face_bounds':[h.x,h.y,h.w,h.h], 'face_crop_bounds':bounds,
                         'locator_score':h.score, 'native_crop_size':list(im.crop(bounds).size)}


def save(im,path,size,quality):
 path=HERE/path
 path.parent.mkdir(parents=True,exist_ok=True)
 assert im.width>=size[0] and im.height>=size[1],(im.size,size)
 im.resize(size,Image.Resampling.LANCZOS).save(path,quality=quality)
 return {'path':str(path.relative_to(HERE)),'sha256':digest(path),'image_size':list(size)}


def main():
 if (HERE/'manifest.json').exists():
  raise ValueError('Dataset already frozen; use a new directory for new generation')
 locator=FaceLocator(HERE/'pr807/models/face_detection_yunet_2023mar.onnx')
 gallery=[];cases=[];annotations=[];review=[]
 for index in range(1,21):
  sheet=(index-1)//5+1;row=(index-1)%5
  ref,ref_ann=panel(f'sheet_{sheet}',row,0)
  ref_face,ref_crop=face_crop(ref,locator)
  query,query_ann=panel('face_queries',row,sheet-1)
  query_face,query_crop=face_crop(query,locator)
  annotations.append({'character_index':index,'reference':ref_ann|ref_crop,'face_query':query_ann|query_crop})
  review.append((index,ref_face,query_face))
  if index in NAMES:
   gallery.append({'label':NAMES[index],'character_index':index,**save(ref_face,Path(f'assets/gallery/{index:02}.jpg'),(128,128),85)})
  for size in [64,32,16]:
   condition=f'face_{size}'
   cases.append({'id':f'{condition}_{index:02}','condition':condition,'character_index':index,
     'expected':NAMES.get(index,'UNKNOWN'),**save(query_face,Path(f'assets/queries/{condition}_{index:02}.jpg'),(size,size),70),
     'processing':query_ann|query_crop})
  for col,view in [(1,'near'),(2,'far')]:
   frame,ann=panel(f'sheet_{sheet}',row,col)
   native_hits=locator.locate(cv2.cvtColor(np.asarray(frame),cv2.COLOR_RGB2BGR))
   ann['source_detected_face_heights']=[h.h for h in native_hits]
   for width in [320,160]:
    condition=f'{view}_{width}'
    cases.append({'id':f'{condition}_{index:02}','condition':condition,'character_index':index,
      'expected':NAMES.get(index,'UNKNOWN'),**save(frame,Path(f'assets/queries/{condition}_{index:02}.jpg'),(width,width*3//4),70),
      'processing':ann,'estimated_face_heights_after_resize':[h.h*(width*3/4)/frame.height for h in native_hits]})
 rng=random.Random(20260910)
 rng.shuffle(cases)
 for case in cases:
  case['gallery_order']=rng.sample(range(15),15)
 manifest={'version':1,'synthetic_only':True,'task':'fictional_character_identity_consistency',
  'seed':20260910,'gallery':gallery,'cases':cases,
  'generated_sources':[{'path':f'assets/sheets/{name}.png','sha256':digest(HERE/'assets/sheets'/f'{name}.png')} for name in LAYOUTS],
  'prepare_sha256':digest(Path(__file__)),'layouts':LAYOUTS,
  'annotation_method':'YuNet boxes on generated source-resolution portraits only; reviewed before predictions; no identity scores used.',
  'unknown_character_indices':[i for i in range(1,21) if i not in NAMES]}
 dump(HERE/'manifest.json',manifest)
 dump(HERE/'annotations.json',annotations)
 # QA contact sheet is not sent to evaluated systems.
 canvas=Image.new('RGB',(4*288,5*162),'white');draw=ImageDraw.Draw(canvas)
 for k,(index,ref,query) in enumerate(review):
  x=(k%4)*288;y=(k//4)*162
  canvas.paste(ref.resize((128,128)),(x,y+24));canvas.paste(query.resize((128,128)),(x+136,y+24))
  draw.text((x+2,y+4),f'{index:02}: {NAMES.get(index,"held-out UNKNOWN")} | ref / query',fill='black')
 canvas.save(HERE/'assets/qa_faces.jpg',quality=92)
 print(f'Frozen {len(gallery)} references and {len(cases)} cases; no source face upscaling.')
 print('Native face-query crop range:',min(a['face_query']['native_crop_size'][0] for a in annotations),max(a['face_query']['native_crop_size'][0] for a in annotations))

if __name__=='__main__':main()
