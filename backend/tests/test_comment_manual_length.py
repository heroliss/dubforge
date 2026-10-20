"""人工超长评论只提示：准备、API预览、确认、正式ASS共享完整文字，不调用真实模型。"""
import copy
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio as cs, comment_preparation as cp
from dubforge.domain import crud
from dubforge.pipeline import comment_overlay as co
from test_comment_studio import uid
from test_comment_preparation import request_body, selected, forbid
from test_subtitle_style import video, pixels


@pytest.mark.parametrize('mode', ['translated', 'original', 'bilingual'])
@pytest.mark.parametrize('independent', [False, True])
def test_manual_long_text_keeps_complete_ass_in_all_display_modes(mode, independent):
    chinese = '完整中文评论' * 48 + '中文结尾'
    original = 'This entire original comment must remain visible. ' * 16 + 'ENDWORD'
    settings = cs.validate_settings(dict(language_mode=mode, source_first=True, source_style_enabled=independent,
        font_size=60, source_font_size=25, width_percent=40, show_author=True, margin=400))
    plan = dict(settings=settings, items=[dict(display_text=chinese, source_text=original,
                                             author='完整作者', selection_origin='manual')])
    before = copy.deepcopy(plan)
    ass = co.ass_document(plan, (640, 360))
    event = next(line.split(',', 9)[-1] for line in ass.splitlines() if line.startswith('Dialogue:'))
    visible = re.sub(r'\{[^}]*\}|\\N|\s', '', event)
    assert '…' not in visible
    assert '完整作者：' in visible
    if mode != 'original':
        assert chinese in visible
    if mode != 'translated':
        assert original.replace(' ', '') in visible
    assert plan == before
    assert ',0,0,72,,' in ass  # 超长人工文字的纵向边距不能把整块文字移出画面。


def test_manual_full_text_layout_stops_at_minimum_and_expands_height():
    parts = ['中文' * 150, 'one two three four ' * 50]
    styles = [dict(font_size=60, min_font_size=24, outline=3),
              dict(font_size=30, min_font_size=20, outline=1)]
    fitted, wrapped, total, widest = co._full_text_layout(parts, styles, 300, 108)
    assert all(''.join(lines).replace(' ', '') == original.replace(' ', '')
               for lines, original in zip(wrapped, parts))
    assert total > 108 and widest <= 300.000001
    assert [style['font_size'] for style in fitted] == [24, 20]


def test_manual_and_automatic_use_same_range_render_fingerprint():
    settings = cs.default_settings()
    normal = dict(settings=settings, items=[dict(display_text='正常文字', selection_origin='manual')])
    automatic = copy.deepcopy(normal); automatic['items'][0]['selection_origin'] = 'automatic'
    assert cs.render_fingerprint_plan(normal) == cs.render_fingerprint_plan(automatic)
    long = copy.deepcopy(normal); long['items'][0]['display_text'] = '字' * 241
    summary = cs.render_fingerprint_plan(long)
    assert 'full_text_layout' not in summary['items'][0]
    assert 'selection_origin' not in summary['items'][0]
    assert summary['render_version'] == 'font-metrics-range-v5'
    assert 'full_text_layout' not in long['items'][0]


@pytest.mark.parametrize('field', ['display_text', 'source_text'])
def test_long_manual_still_rejects_empty_selected_language(uid, field):
    settings = {**cs.view(uid)['defaults'], 'language_mode':'bilingual'}
    item = selected(uid, ['0'], text='字' * 241)[0]
    item['source_text'] = 'original ' * 100
    item[field] = ' '
    with pytest.raises(cs.CommentError) as error:
        cs.draft(uid, dict(settings=settings, items=[item]))
    assert error.value.code == 'COMMENTS_DISPLAY_TEXT'


@pytest.mark.parametrize('field,value', [('display_text', '字' * 15001), ('source_text', 'a' * 12001),
                                         ('display_text', None)], ids=['translated-input-limit', 'source-input-limit', 'invalid-type'])
def test_manual_length_advice_never_bypasses_input_safety(uid, field, value):
    items = selected(uid, ['0']); items[0][field] = value
    with pytest.raises(cs.CommentError) as error:
        cs.draft(uid, dict(settings=cs.view(uid)['defaults'], items=items))
    assert error.value.code == 'COMMENTS_DISPLAY_TEXT'


def test_manual_overlong_content_review_remains_blocking(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    items = selected(uid, ['0'], text='字' * 241 + '天安门广场的悲剧')
    prepared = cp.prepare(uid, request_body(uid, 1, items=items))['preparation']
    assert prepared['summary']['invalid_manual'] == 1 and prepared['summary']['ready'] == 0
    with pytest.raises(cs.CommentError) as error:
        cs.draft(uid, prepared['draft'])
    assert error.value.code == 'COMMENTS_MANUAL_INVALID'


def test_api_manual_overlong_preview_save_and_final_ass_share_complete_text(uid, video, monkeypatch, tmp_path):
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    from dubforge.pipeline import video_source
    monkeypatch.setattr(video_source, 'resolve_video', lambda *args: video)
    state = cs.view(uid)
    settings = {**state['defaults'], 'start':0, 'language_mode':'bilingual', 'font':'Arial',
                'source_font':'Arial', 'source_style_enabled':True, 'width_percent':80,
                'margin':20, 'color':'#FF0000', 'source_color':'#FF0000', 'background_opacity':0}
    item = selected(uid, ['0'], text='完整中文评论' * 42 + '中文结尾')[0]
    item['source_text'] = 'The complete original comment remains visible. ' * 16 + 'ENDWORD'
    value = dict(settings=settings, items=[item])
    client = TestClient(app)
    response = client.post(f'/api/projects/{uid}/comments/prepare', json=dict(value, target=1, prompt=cs.FILTER_PROMPT))
    assert response.status_code == 200
    prepared = response.json()['preparation']
    assert prepared['summary']['ready'] == 1 and prepared['invalid_manual'] == []
    preview = client.post(f'/api/projects/{uid}/comments/preview', json=dict(value, seconds=1))
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/png'
    (tmp_path / 'manual-preview.png').write_bytes(preview.content)
    frame = pixels(preview.content)
    ys, xs = np.where((frame[:,:,0] > 100) & (frame[:,:,1] < 80))
    # 评论画布统一为 1080 高；20 逻辑像素在 640×360 预览中约为 6.7 像素。
    assert len(xs) > 100 and xs.min() >= 5 and xs.max() < 635
    assert ys.min() >= 5 and ys.max() < 360
    saved = client.put(f'/api/projects/{uid}/comments/overlay', json=dict(value, revision=state['overlay_revision']))
    assert saved.status_code == 200
    confirmed = cs.confirmed(uid)
    assert confirmed['items'][0]['display_text'] == item['display_text']
    assert confirmed['items'][0]['source_text'] == item['source_text']
    final_ass = co.ass_document(confirmed, (640,360))
    visible = re.sub(r'\{[^}]*\}|\\N|\s', '', final_ass)
    assert item['display_text'] in visible and item['source_text'].replace(' ','') in visible
    assert '…' not in final_ass


def test_full_manual_wide_letters_remain_inside_preview(video):
    settings = cs.validate_settings(dict(start=0, position='top_left', width_percent=40,
        font='Arial', margin=20, show_author=False, color='#FF0000', outline=0,
        background_opacity=0, language_mode='original', font_size=60, min_font_size=30, height_percent=10))
    plan = dict(settings=settings, items=[dict(source_text='MWWM ' * 65 + 'END', selection_origin='manual')])
    frame = pixels(co.preview_frame(video, plan, 1))
    ys, xs = np.where((frame[:,:,0] > 100) & (frame[:,:,1] < 80))
    assert len(xs) > 100
    assert xs.min() >= 5 and xs.max() <= 640 * .4 + 10
    assert ys.min() >= 5 and ys.max() < 360


def test_layout_details_reports_soft_height_and_video_overflow_separately():
    settings = cs.validate_settings(dict(start=0, interval=15, duration=12, show_author=False,
        font_size=36, min_font_size=24, width_percent=40, height_percent=10))
    plan = dict(settings=settings, items=[dict(display_text='完整中文' * 35, selection_origin='manual')])
    normal = co.layout_details(plan, (1920, 1080), 0)
    assert normal['height_exceeded'] and normal['min_reached']
    assert not normal['video_height_exceeded']
    assert normal['font_sizes'] == [24]
    plan['items'][0]['display_text'] *= 20
    overflow = co.layout_details(plan, (1920, 1080), 0)
    assert overflow['video_height_exceeded'] and overflow['font_sizes'] == [24]
    assert co.layout_details(plan, (1920, 1080), 12) is None


@pytest.mark.parametrize('position', ['top_left', 'bottom_left'])
def test_real_preview_expands_soft_height_and_keeps_configured_minimum(video, position):
    settings = cs.validate_settings(dict(start=0, show_author=False, width_percent=40, height_percent=10,
        font='Arial', font_size=60, min_font_size=30, margin=20, language_mode='original',
        color='#FF0000', outline=0, background_opacity=0, position=position))
    plan = dict(settings=settings, items=[dict(source_text='MWWM ' * 65 + 'END')])
    ass = co.ass_document(plan, co.video_canvas(video))
    assert r'\fs30' in ass and '…' not in ass and 'END' in ass
    frame = pixels(co.preview_frame(video, plan, 1))
    ys, xs = np.where((frame[:, :, 0] > 100) & (frame[:, :, 1] < 80))
    assert len(xs) > 100
    assert xs.min() >= 5 and xs.max() <= 640 * .4 + 10
    assert np.ptp(ys) > 360 * .10 + 5
    assert ys.max() < 360
    assert (ys.min() < 15 if position == 'top_left' else ys.max() > 345)
