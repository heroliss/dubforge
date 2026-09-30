"""只用已保存的参考和参数对照生成，不改项目配置、音频版本或发布状态。"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.dubbing_edit import read_take
from dubforge.backends.tts_indextts25 import synth
from dubforge.utils.fs import atomic_write_bytes

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project', required=True)
parser.add_argument('--lines', type=int, nargs='+', required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
project = crud.get_project(args.project)
deps = Dependencies(project)
rows = {line['id']: line for line in deps.dubbing_lines()}
args.output.mkdir(parents=True, exist_ok=True)
for number in args.lines:
    line = rows[number]
    take = read_take(deps.root, line, 1)
    settings = take['settings']
    refs = deps.root / 'artifacts' / 'tts' / 'refs' / take['fingerprint']
    timbre = refs / f'ref_timbre_{number}.wav'
    emotion = refs / f'ref_emo_{number}.wav'
    if not timbre.is_file() or not emotion.is_file():
        raise RuntimeError('未找到本句当时使用的动态参考，不自动替换参考或修改项目')
    output = args.output / f'{number}.wav'
    if output.exists():
        raise RuntimeError('对照输出已存在，请选择新的诊断目录')
    print(f'句 {number} 开始对照，文本 {line["text"]}，时长因子 {settings["duration_factor"]}', flush=True)
    started = time.monotonic()
    synth(settings['url'], line['text'], output, settings['lang'], timbre_ref=timbre,
          emo_mode=settings['emo_mode'], emo_ref=emotion, emo_weight=settings['emo_weight'],
          duration_factor=settings['duration_factor'], advanced=settings,
          timeout_seconds=120, log=lambda text: print(text, flush=True))
    elapsed = round(time.monotonic() - started, 2)
    result = {'line': number, 'seconds': elapsed, 'original_fingerprint': take['fingerprint'],
              'file': str(output), 'parameters_changed': False}
    atomic_write_bytes(output.with_suffix('.json'), json.dumps(result, ensure_ascii=False).encode())
    print(json.dumps(result, ensure_ascii=False), flush=True)
