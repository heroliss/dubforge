"""Windows 当前用户 DPAPI 保护；不把 Cookie 写入项目或明文配置。"""
import base64
import ctypes
import json
import os
from ctypes import wintypes

from dubforge.errors import DubForgeError


class CredentialError(DubForgeError):
    code = 'PUBLISH_CREDENTIAL'
    suggestion = '请在发布中心重新扫码连接账号。'


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise CredentialError('当前凭据存储仅支持 Windows，未保存任何明文登录凭据')
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    method = library.CryptUnprotectData if decrypt else library.CryptProtectData
    method.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    method.restype = wintypes.BOOL
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise CredentialError('无法读取或保护账号凭据，请在当前 Windows 用户下重新登录')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def protect(value: dict) -> str:
    return base64.b64encode(_crypt(json.dumps(value).encode())).decode()


def unprotect(value: str) -> dict:
    try:
        return json.loads(_crypt(base64.b64decode(value), decrypt=True))
    except CredentialError:
        raise
    except Exception:
        raise CredentialError('账号凭据已损坏，请重新扫码') from None
