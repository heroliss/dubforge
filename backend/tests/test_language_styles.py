"""原文/译文样式的真实渲染、继承及临时产物验证；不接触用户视频。"""
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from dubforge import comment_studio as cs, configuration
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import comment_overlay, subtitle_style, step_assemble, step_subtitles, assembly_versions
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.srt import SubLine, write_srt
from test_configuration import save, bind
from test_subtitle_style import cfg, pixels, video


def subtitle_settings():
    return {**subtitle_style.STYLE_DEFAULTS, 'assemble.subtitle_track':'bilingual',
        'assemble.subtitle_source_style_enabled':True, 'assemble.subtitle_font':'Arial',
        'assemble.subtitle_source_font':'Arial', 'assemble.subtitle_size':72,
        'assemble.subtitle_source_size':36, 'assemble.subtitle_color':'#FF0000',
        'assemble.subtitle_source_color':'#0000FF', 'assemble.subtitle_source_outline_color':'#00FF00',
        'assemble.subtitle_source_outline':1, 'assemble.bgm_volume':0}


def bilingual_line(source_first=False):
    parts = (('trans','译文更大'), ('source','Original smaller'))
    if source_first:
        parts = parts[::-1]
    return SubLine(1, .5, 2.5, '\n'.join(text for _, text in parts), parts)


@pytest.mark.parametrize('kind', ['subtitle','comment'])
@pytest.mark.parametrize('source_first', [False,True])
def test_real_language_size_color_and_order(video, kind, source_first):
    if kind == 'subtitle':
        data = subtitle_style.preview_frame(video, [bilingual_line(source_first)], cfg(subtitle_settings()), 1)
    else:
        settings = {**cs.DEFAULT_SETTINGS, 'language_mode':'bilingual', 'source_first':source_first,
            'start':.5, 'duration':2, 'font':'Arial', 'source_font':'Arial', 'font_size':72, 'source_font_size':36,
            'source_style_enabled':True, 'color':'#FF0000', 'source_color':'#0000FF',
            'source_outline_color':'#00FF00', 'source_outline':1, 'background_opacity':0}
        data = comment_overlay.preview_frame(video, {'settings':settings,'items':[{'display_text':'译文更大','source_text':'Original smaller'}]}, 1)
    rgb = pixels(data).astype(int)
    red = (rgb[:,:,0] > 120) & (rgb[:,:,1] < 80) & (rgb[:,:,2] < 80)
    blue = (rgb[:,:,2] > 60) & (rgb[:,:,2] > rgb[:,:,1] + 20) & (rgb[:,:,2] > rgb[:,:,0] + 30)
    green = (rgb[:,:,1] > 70) & (rgb[:,:,1] > rgb[:,:,0] + 30) & (rgb[:,:,1] > rgb[:,:,2] + 30)
    assert red.sum() > 70 and blue.sum() > 35 and green.sum() > 10
    yr, yb = np.where(red)[0], np.where(blue)[0]
    assert (yb.mean() < yr.mean()) == source_first
    assert np.ptp(yr) > np.ptp(yb) * 1.3


def test_ass_preserves_multiline_language_identity_and_escapes_text():
    parts = (('source','line one\nline two {\\pos(0,0)}'), ('trans','译文\n第二行'))
    text = subtitle_style.ass_document([SubLine(1, 1, 4, 'unused', parts)], cfg(subtitle_settings()), (1920,1080), 2, 3)
    assert '0:00:00.00,0:00:01.00' in text
    event = next(line for line in text.splitlines() if line.startswith('Dialogue:'))
    assert event.count('\\fs36') == 1 and event.count('\\fs72') == 1
    assert 'line one\\Nline two ｛＼pos(0,0)｝' in event
    assert '\\pos(' not in event and event.index('\\fs36') < event.index('\\fs72')
    with pytest.raises(DubForgeError, match='分段信息'):
        subtitle_style.ass_document([SubLine(1,1,2,'不能猜换行')], cfg(subtitle_settings()), (1920,1080))


@pytest.mark.parametrize('key,value', [('source_font', "Arial,Injected"), ('source_font_size', 0),
    ('source_color','red'), ('source_outline_color',None), ('source_outline', float('nan')), ('source_style_enabled','true')])
def test_comment_and_subtitle_validate_independent_fields(key,value):
    with pytest.raises(DubForgeError): cs.validate_settings({key:value})
    from dubforge.pipeline.step_params import validate_numeric_updates
    subtitle_key = key.replace('font_size','size')
    with pytest.raises(DubForgeError): validate_numeric_updates({'assemble.subtitle_'+subtitle_key:value})


@pytest.mark.parametrize('dub', [False,True])
@pytest.mark.parametrize('start', [None,1.0])
def test_real_final_and_clip_burn_share_ass_and_cleanup(video,tmp_path,monkeypatch,dub,start):
    # 本用例只验证样式与时钟，SRT 夹具不提供内容审查需要的整理稿。
    settings = {**subtitle_settings(), 'content_review.enabled': False}
    line = bilingual_line()
    source = tmp_path / 'subtitles.srt'
    write_srt([line],source)
    before = source.read_bytes()
    monkeypatch.setattr(step_subtitles,'build_tracks',lambda *a,**k:({'bilingual.srt':[line]},[]))
    commands=[]
    def run(cmd, cwd=None, **kwargs):
        commands.append(cmd)
        result=subprocess.run(cmd,cwd=cwd,capture_output=True,timeout=30)
        return result.returncode,result.stdout.decode('utf-8','replace'),result.stderr.decode('utf-8','replace')
    ctx=SimpleNamespace(cfg=cfg(settings),upstream=lambda *a:source,run_subprocess=run,task_data={},
        progress=lambda *a,**k:None,log=lambda *a:None)
    out=tmp_path/'output'/'final.mp4'
    if dub:
        voice=tmp_path/'voice.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','anullsrc=r=48000:cl=mono','-t','3',str(voice)],check=True)
        step_assemble._mux(ctx,tmp_path,video,voice,out,1.0 if start else 3.0,burn=True,start=start)
    else:
        step_assemble._mux_original_sub(ctx,video,out,1.0 if start else 3.0,(0,99),burn=True,start=start)
    assert out.is_file() and not list(tmp_path.glob('.subtitle_*.ass'))
    assert source.read_bytes() == before
    result=subprocess.run(['ffmpeg','-v','error','-ss','0.7' if start is None else '0.2','-i',str(out),
        '-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],capture_output=True,check=True)
    rgb=pixels(result.stdout).astype(int)
    assert ((rgb[:,:,0]>150)&(rgb[:,:,2]<70)).sum()>50
    assert ((rgb[:,:,2]>120)&(rgb[:,:,0]<70)).sum()>25
    assert any('ass=.subtitle_' in arg for cmd in commands for arg in cmd)


def test_styles_inherit_global_series_project_and_version(make_project):
    uid=make_project()
    global_fields={'assemble.subtitle_source_style_enabled':True,'assemble.subtitle_source_size':34,
                   'comments.source_style_enabled':True,'comments.source_font_size':28}
    save('global',global_fields)
    series=configuration.create_template('双语系列')
    save(series['key'],{'assemble.subtitle_source_color':'#88BBFF','comments.source_color':'#99FFCC'})
    bind([uid],series)
    save('project:'+uid,{'assemble.subtitle_source_size':38,'comments.source_font_size':32})
    values=configuration.resolve(uid)['values']
    assert values['assemble.subtitle_source_color']=='#88BBFF' and values['comments.source_font_size']==32
    version=dict(id='p1',name='独立版本',dubbing=False,burn_subtitles=True,include_comments=True,
        overrides={'assemble.subtitle_source_size':42,'comments.source_color':'#FFCC66'})
    result=assembly_versions.resolve(Dependencies(crud.get_project(uid)),version,
        comment_plan={'settings':cs.project_settings(uid),'items':[]})
    assert result['values']['assemble.subtitle_source_size']==42
    assert result['comment_overlay']['settings']['source_color']=='#FFCC66'
    assert cs.project_settings(uid)['source_color']=='#99FFCC'


def test_inactive_source_style_does_not_invalidate_outputs(make_project):
    from test_assembly_versions import definition
    from dubforge.runner import compute_fingerprint
    uid=make_project()
    deps=Dependencies(crud.get_project(uid))
    version=definition(**{'assemble.subtitle_track':'bilingual'})
    before=assembly_versions.fingerprint(deps,assembly_versions.resolve(deps,version))
    step_before=compute_fingerprint(deps.project,'assemble',dependencies=deps)
    save('project:'+uid,{'assemble.subtitle_source_color':'#88FFAA','assemble.subtitle_source_size':32})
    deps=Dependencies(crud.get_project(uid))
    assert assembly_versions.fingerprint(deps,assembly_versions.resolve(deps,version))==before
    assert compute_fingerprint(deps.project,'assemble',dependencies=deps)==step_before
    save('project:'+uid,{'assemble.subtitle_source_style_enabled':True})
    deps=Dependencies(crud.get_project(uid))
    assert assembly_versions.fingerprint(deps,assembly_versions.resolve(deps,version))!=before
    old={'settings':{key:value for key,value in cs.DEFAULT_SETTINGS.items() if key!='font' and not (key.startswith('source_') and key!='source_first')},'items':[]}
    new={'settings':dict(cs.DEFAULT_SETTINGS,source_color='#AAFFCC'),'items':[]}
    assert cs.render_fingerprint_plan(new)==cs.render_fingerprint_plan(old)


@pytest.mark.parametrize('track', ['source','trans'])
def test_single_language_uses_its_own_style_and_toggle_off_follows_common(video,track):
    settings={**subtitle_settings(),'assemble.subtitle_track':track}
    line=SubLine(1,.5,2.5,'Single line')
    on=pixels(subtitle_style.preview_frame(video,[line],cfg(settings),1)).astype(int)
    off=pixels(subtitle_style.preview_frame(video,[line],cfg({**settings,'assemble.subtitle_source_style_enabled':False}),1)).astype(int)
    blue=lambda rgb: ((rgb[:,:,2]>60)&(rgb[:,:,2]>rgb[:,:,0]+30)).sum()
    assert (blue(on)>20)==(track=='source')
    assert blue(off)==0
