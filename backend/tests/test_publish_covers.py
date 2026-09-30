"""封面适配不联网，不更改原图，失败不留下最终名。"""
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from dubforge import config
from dubforge.publishing import covers

WEBP = b'RIFF\x00\x00\x00\x00WEBPfixture'
PNG = b'\x89PNG\r\n\x1a\nfixtureIEND\xaeB`\x82'


def test_webp_converts_once_with_input_fingerprint_and_preserves_source(isolated_env, monkeypatch):
    source = config.data_root() / 'original.webp'
    source.write_bytes(WEBP)
    calls = []
    def convert(args, **kwargs):
        calls.append(args)
        assert kwargs['timeout'] == 30 and '-nostdin' in args
        assert args[args.index('-protocol_whitelist')+1] == 'file,pipe'
        return SimpleNamespace(returncode=0,stdout=PNG)
    monkeypatch.setattr(covers.subprocess,'run',convert)
    first = covers.upload_path(source)
    assert first.suffix == '.png' and first.read_bytes() == PNG and source.read_bytes() == WEBP
    assert covers.upload_path(source) == first and len(calls) == 1
    source.write_bytes(WEBP + b'changed')
    assert covers.upload_path(source) != first and len(calls) == 2


@pytest.mark.parametrize('suffix,data',[('.png',PNG),('.jpg',b'\xff\xd8\xffimage'),('.jpeg',b'\xff\xd8\xffimage')])
def test_supported_formats_passthrough(isolated_env,monkeypatch,suffix,data):
    source=config.data_root()/('image'+suffix);source.write_bytes(data)
    monkeypatch.setattr(covers.subprocess,'run',lambda *a,**k:pytest.fail('无需转换'))
    assert covers.upload_path(source)==source


@pytest.mark.parametrize('result',[SimpleNamespace(returncode=1,stdout=PNG),SimpleNamespace(returncode=0,stdout=b'partial')])
def test_conversion_failure_does_not_publish_partial_file(isolated_env,monkeypatch,result):
    source=config.data_root()/'image.webp';source.write_bytes(WEBP)
    monkeypatch.setattr(covers.subprocess,'run',lambda *a,**k:result)
    with pytest.raises(covers.UploadCoverError):covers.upload_path(source)
    assert not list((config.data_root()/'publishing/upload-covers').glob('*'))
    assert source.read_bytes()==WEBP


def test_conversion_timeout_and_invalid_input_are_domain_errors(isolated_env,monkeypatch):
    source=config.data_root()/'image.webp';source.write_bytes(WEBP)
    def timeout(*a,**k):raise subprocess.TimeoutExpired('ffmpeg',30)
    monkeypatch.setattr(covers.subprocess,'run',timeout)
    with pytest.raises(covers.UploadCoverError,match='超时'):covers.upload_path(source)
    source.write_bytes(PNG)
    with pytest.raises(covers.UploadCoverError,match='不匹配'):covers.upload_path(source)


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='需要FFmpeg验证真实转码')
def test_real_ffmpeg_webp_becomes_png(isolated_env):
    image=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',
        'color=c=orange:s=48x32','-frames:v','1','-c:v','libwebp','-f','image2pipe','pipe:1'],
        capture_output=True,check=True,timeout=15).stdout
    source=config.data_root()/'actual.webp';source.write_bytes(image)
    output=covers.upload_path(source)
    assert output.read_bytes().startswith(b'\x89PNG\r\n\x1a\n') and source.read_bytes()==image
