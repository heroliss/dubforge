"""既有制作输入表示的规范摘要；纯函数，不读取配置、文件或运行器。"""
import hashlib
import json


def fingerprint_digest(payload: dict) -> str:
    """序列化字节保持与既有制作指纹一致，不修改传入表示。"""
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()
