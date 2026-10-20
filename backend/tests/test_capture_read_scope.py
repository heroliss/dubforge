"""输入说明与指纹共用一次读取，但后续调用和并发文件变化仍须重新核验。"""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from conftest import mark_upstream_done
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, stale_explanation as stale
from dubforge.pipeline.dependencies import Dependencies
from dubforge import runner as runner_module
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.input_fingerprint import fingerprint_digest


def old_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.fixture
def prepared(make_project):
    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    return Dependencies(crud.get_project(uid))


@pytest.mark.parametrize('options', [{}, {'legacy':True}, {'legacy_config':True},
    {'legacy_execution':True}, {'legacy':True,'legacy_config':True},
    {'legacy':True,'legacy_execution':True}])
def test_digest_and_compute_preserve_all_existing_serialization_bytes(prepared, options):
    for key in ('split','translate','subtitles','assemble'):
        payload = runner_module.fingerprint_payload(prepared.project,key,dependencies=prepared,**options)
        untouched = copy.deepcopy(payload)
        assert fingerprint_digest(payload) == old_digest(payload)
        assert runner_module.compute_fingerprint(prepared.project,key,dependencies=prepared,**options) == old_digest(payload)
        assert payload == untouched


@pytest.mark.parametrize('scope,provided', [('full',False),('outputs',False),('outputs',True)])
def test_capture_full_and_outputs_match_previous_result_with_one_payload_read(prepared, monkeypatch, scope, provided):
    payload = runner_module.fingerprint_payload(prepared.project,'assemble',dependencies=prepared)
    expected_entries = stale._payload_entries(prepared,'assemble',payload,current=True)
    expected_digest = old_digest(payload)
    arguments = {'fingerprint':'outputs:'+expected_digest} if provided else {}
    calls = []
    original = runner_module.fingerprint_payload
    def read(*args, **kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(runner_module,'fingerprint_payload',read)
    result = stale.capture(prepared,'assemble',scope=scope,_current=True,**arguments)
    assert len(calls) == 1
    assert result['entries'] == expected_entries
    assert result['fingerprint'] == ('outputs:' if provided else '')+expected_digest
    assert result['scope'] == scope
    assert result['selection'] == ({'variants':prepared.assemble_variants} if scope=='outputs' else {})


@pytest.mark.parametrize('change', ['rewrite','replace','restored-mtime','delete'])
def test_next_capture_on_same_dependencies_rechecks_changed_take(prepared, change):
    row = prepared.dubbing_lines(required=True)[0]
    path = dubbing_edit.take_path(prepared.root,row['id']).with_suffix('.take.json')
    first = stale.capture(prepared,'subtitles')
    before = path.stat()
    changed = json.loads(path.read_text(encoding='utf-8'))
    changed['identity'] = '0'*64
    encoded = json.dumps(changed,ensure_ascii=False).encode()
    if change == 'delete':
        path.unlink()
    elif change == 'replace':
        atomic_write_bytes(path,encoded)
    else:
        assert len(encoded) == before.st_size
        path.write_bytes(encoded)
        if change == 'restored-mtime':
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    second = stale.capture(prepared,'subtitles')
    assert first['fingerprint'] != second['fingerprint']
    assert runner_module.fingerprint_payload(prepared.project,'subtitles',dependencies=prepared)['voice_placement']['voices'][0]['version'] is None


def test_take_replacement_during_identity_read_cannot_reuse_previous_valid_voice(prepared, monkeypatch):
    row = prepared.dubbing_lines(required=True)[0]
    path = dubbing_edit.take_path(prepared.root,row['id']).with_suffix('.take.json')
    first = stale.capture(prepared,'subtitles')
    changed = json.loads(path.read_text(encoding='utf-8'))
    changed['identity'] = '0'*64
    original_open = Path.open
    replaced = []
    def open_with_replacement(candidate, *args, **kwargs):
        if candidate == path and not replaced:
            replaced.append(1)
            atomic_write_bytes(path,json.dumps(changed,ensure_ascii=False).encode())
        return original_open(candidate,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_with_replacement)
    current = stale.capture(prepared,'subtitles')
    assert replaced and current['fingerprint'] != first['fingerprint']
    assert runner_module.fingerprint_payload(prepared.project,'subtitles',dependencies=prepared)['voice_placement']['voices'][0]['version'] is None


def test_capture_does_not_hide_input_read_failure_or_return_cached_digest(prepared, monkeypatch):
    stale.capture(prepared,'subtitles')
    def unreadable(*_args,**_kwargs):
        raise OSError('隔离文件在读取过程中变化')
    monkeypatch.setattr(runner_module,'fingerprint_payload',unreadable)
    with pytest.raises(OSError,match='读取过程中变化'):
        stale.capture(prepared,'subtitles')


def test_new_capture_can_use_resident_old_runner_without_new_digest_symbol(prepared, monkeypatch):
    monkeypatch.delattr(runner_module,'fingerprint_digest',raising=False)
    result = stale.capture(prepared,'subtitles')
    payload = runner_module.fingerprint_payload(prepared.project,'subtitles',dependencies=prepared)
    assert result['fingerprint'] == old_digest(payload)


def test_entries_and_digest_use_one_coherent_payload_while_next_call_rechecks(prepared, monkeypatch):
    first = stale.capture(prepared,'subtitles')
    row = prepared.dubbing_lines(required=True)[0]
    path = dubbing_edit.take_path(prepared.root,row['id']).with_suffix('.take.json')
    original = stale._payload_entries
    def describe(*args,**kwargs):
        entries = original(*args,**kwargs)
        changed = json.loads(path.read_text(encoding='utf-8'))
        changed['identity'] = '0'*64
        atomic_write_bytes(path,json.dumps(changed,ensure_ascii=False).encode())
        return entries
    monkeypatch.setattr(stale,'_payload_entries',describe)
    captured = stale.capture(prepared,'subtitles')
    assert captured['entries'] == first['entries']
    assert captured['fingerprint'] == first['fingerprint']
    monkeypatch.setattr(stale,'_payload_entries',original)
    assert stale.capture(prepared,'subtitles')['fingerprint'] != captured['fingerprint']
