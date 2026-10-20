"""当前帧排版提示与真实预览方案共用，空档不携带上一条的状态。"""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from dubforge import comment_studio as cs
from dubforge import comment_preview
from dubforge.pipeline import comment_overlay as co, video_source
from test_comment_studio import seed

def test_preview_exposes_active_layout_and_omits_gap(make_project,monkeypatch):
 uid=make_project();seed(uid,2)
 rows=cs.view(uid)['rows'];row=rows[0]
 monkeypatch.setattr(video_source,'resolve_video',lambda *args:Path('fixture.mp4'))
 probes=[];layouts=[]
 monkeypatch.setattr(co,'_run',lambda *args,**kwargs:b'fixture-frame')
 monkeypatch.setattr(co,'video_canvas',lambda *args:probes.append(args) or (1920,1080))
 original_layout=co._item_layout
 def item_layout(*args):
  layouts.append(args)
  return original_layout(*args)
 monkeypatch.setattr(co,'_item_layout',item_layout)
 settings={**cs.view(uid)['defaults'],'start':0,'duration':1,'interval':2,'font_size':18,'min_font_size':18,'height_percent':1,'language_mode':'translated'}
 item={**row,'source_text':row['text'],'display_text':'完整手选评论。'*80,'selection_origin':'manual'}
 next_item={**rows[1],'source_text':rows[1]['text'],'display_text':'下一条评论','selection_origin':'manual'}
 body=dict(settings=settings,items=[item,next_item],seconds=0)
 client=TestClient(app)
 response=client.post(f'/api/projects/{uid}/comments/preview',json=body)
 assert response.status_code==200 and response.content==b'fixture-frame'
 details=json.loads(response.headers['X-DubForge-Comment-Layout'])
 assert details['font_sizes']==[18]
 assert details['height_exceeded'] and details['min_reached']
 assert not details['video_height_exceeded']
 # 同一 API 请求只探测一次画布，图像与响应头复用同一次当前评论排版。
 assert len(probes)==1 and len(layouts)==1
 # 即使距下一条不足取帧窗口的 0.1 秒，也不提前为下一条排版。
 near_end=client.post(f'/api/projects/{uid}/comments/preview',json={**body,'seconds':.95})
 assert near_end.status_code==200
 assert len(probes)==2 and len(layouts)==2
 gap=client.post(f'/api/projects/{uid}/comments/preview',json={**body,'seconds':1.95})
 assert gap.status_code==200 and 'X-DubForge-Comment-Layout' not in gap.headers
 assert len(probes)==3 and len(layouts)==2


def test_preview_busy_keeps_stable_code_and_retry_message(make_project,monkeypatch):
 uid=make_project();seed(uid,1)
 state=cs.view(uid);row=state['rows'][0]
 monkeypatch.setattr(video_source,'resolve_video',lambda *args:Path('fixture.mp4'))
 def busy(*args):
  raise cs.CommentError('其他预览正在生成，请稍后重试',code='COMMENTS_PREVIEW_BUSY')
 monkeypatch.setattr(comment_preview,'render',busy)
 body=dict(settings=state['defaults'],items=[{**row,'source_text':row['text'],'display_text':'有效评论','selection_origin':'manual'}],seconds=0)
 response=TestClient(app).post(f'/api/projects/{uid}/comments/preview',json=body)
 assert response.status_code==400
 assert response.json()['code']=='COMMENTS_PREVIEW_BUSY'
 assert response.json()['message']=='其他预览正在生成，请稍后重试'
