"""逐句朗读文本与字幕分离；原样使用用户指定的模型输入。"""

from dubforge.errors import DubForgeError
from dubforge.pipeline.step_params import ParamField

PATH = 'tts.indextts25.pronunciation_text'
KEY = 'pronunciation_text'
FIELD = ParamField(PATH, '朗读文本', 'text', group='朗读内容',
    help='可自由修改标点、数字写法、措辞或整句话，保存后作为本句实际送给 IndexTTS 的文字；字幕不变。'
         '注音标记是可选的，例如 <朝|CHAO2>；留空时跟随本句译文。')


class PronunciationError(DubForgeError):
    code = 'TTS_PRONUNCIATION_INVALID'
    suggestion = '请输入朗读文本；留空可恢复跟随本句译文。'


def validate(value, text):
    """只核对传输类型；标点、措辞和可选模型标记均由用户决定。"""
    if not isinstance(value, str):
        raise PronunciationError('朗读文本必须是文字，留空可恢复跟随译文。')
    # 保留内部空白和标记大小写；非空即独立设置，相同译文也不会被自动清除。
    return value.strip()


def spoken_text(line, cfg):
    """合成、指纹与重试共同使用同一份有效朗读输入。"""
    value = validate(cfg(PATH, ''), line['text'])
    return value or line['text']


def preserve_records(records):
    """统一声音参数也保留独立朗读文本；空记录不影响历史输入快照。"""
    return {ident: {**record, 'settings': {PATH: record['settings'][PATH]}}
            for ident, record in records.items()
            if isinstance(record, dict) and isinstance(record.get('settings'), dict)
            and record['settings'].get(PATH)}
