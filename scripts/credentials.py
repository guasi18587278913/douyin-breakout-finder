"""Private, opt-in local TikHub credentials. No network, logging, or model access."""
import base64
import ctypes
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from core import StopRun

NAME = 'tikhub.json'
MAX_BYTES = 16384


class CredentialError(StopRun):
    pass


def validate_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise CredentialError('Key 格式不正确，请重新复制。')
    return value.strip()


def windows_crypt(data, decrypt=False):
    """Use Windows DPAPI for the current user, never the machine-wide flag."""
    if os.name != 'nt':
        raise CredentialError('这个保存文件需要在原 Windows 用户账户中读取，请重新填写 Key。')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(b'douyin-breakout-finder/tikhub/v1')
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = Blob(len(entropy_buffer.raw)-1, ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(output)):
            raise CredentialError('系统未能保护或读取 Key，请重新填写，或取消“记住 Key”后仅本轮使用。')
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        if output.pbData:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel32.LocalFree(output.pbData)


class CredentialStore:
    def __init__(self, directory, forbidden_roots=()):
        self.root = Path(directory).absolute()
        actual = self.root.resolve()
        self.available = not any(actual == Path(p).resolve() or Path(p).resolve() in actual.parents for p in forbidden_roots)

    def exists(self):
        if not self.available:
            return False
        try:
            (self.root / NAME).lstat()
            return True
        except OSError:
            return False

    @staticmethod
    def _is_link(info):
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

    @contextmanager
    def _directory(self, create=False, private=True):
        if not self.available:
            raise CredentialError('当前数据目录位于技能源码内，不能在这里记住 Key。请使用默认数据目录。')
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if self._is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise CredentialError('密钥目录不是安全的普通文件夹，请检查后重试。')
        if os.name != 'nt' and (info.st_uid != os.getuid() or (private and stat.S_IMODE(info.st_mode) & 0o077)):
            raise CredentialError('密钥目录的访问权限不正确，暂时无法保存或读取。')
        handle = None
        if os.name != 'nt':
            handle = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened = os.fstat(handle)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                os.close(handle)
                raise CredentialError('密钥目录发生变化，请重试。')
        try:
            yield handle
        finally:
            if handle is not None:
                os.close(handle)

    def _open(self, name, flags, handle, mode=0o600):
        flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        return os.open(name, flags, mode, dir_fd=handle) if handle is not None else os.open(self.root / name, flags, mode)

    def _unlink(self, name, handle):
        if handle is None:
            (self.root / name).unlink(missing_ok=True)
        else:
            try: os.unlink(name, dir_fd=handle)
            except FileNotFoundError: pass

    def load(self):
        if not self.available:
            return None
        try:
            with self._directory() as handle:
                before = (self.root / NAME).lstat()
                if self._is_link(before) or not stat.S_ISREG(before.st_mode):
                    raise CredentialError('保存的 Key 文件不安全，请先忘记后重新填写。')
                with os.fdopen(self._open(NAME, os.O_RDONLY, handle), 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                        raise CredentialError('保存的 Key 文件不安全，请先忘记后重新填写。')
                    if os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                        raise CredentialError('保存的 Key 文件权限不正确，请先忘记后重新填写。')
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('oversized')
                saved = json.loads(raw)
                if not isinstance(saved, dict) or set(saved) != {'version', 'protection', 'value'} or saved['version'] != 1:
                    raise ValueError('invalid format')
                if saved['protection'] == 'windows-dpapi':
                    value = windows_crypt(base64.b64decode(saved['value'], validate=True), decrypt=True).decode('ascii')
                elif saved['protection'] == 'owner-file' and os.name != 'nt':
                    value = saved['value']
                else:
                    raise ValueError('wrong platform')
                return validate_key(value)
        except FileNotFoundError:
            return None
        except CredentialError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            raise CredentialError('保存的 Key 无法读取，请先忘记后重新填写。') from None

    def save(self, key):
        key = validate_key(key)
        try:
            if os.name == 'nt':
                protection = 'windows-dpapi'
                value = base64.b64encode(windows_crypt(key.encode('ascii'))).decode('ascii')
            else:
                protection, value = 'owner-file', key
            raw = json.dumps({'version': 1, 'protection': protection, 'value': value}).encode('ascii')
            with self._directory(create=True) as handle:
                name = '.key-' + secrets.token_hex(12) + '.tmp'
                try:
                    with os.fdopen(self._open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, handle), 'wb') as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    if handle is None:
                        os.replace(self.root / name, self.root / NAME)
                    else:
                        os.replace(name, NAME, src_dir_fd=handle, dst_dir_fd=handle)
                        os.fsync(handle)
                finally:
                    self._unlink(name, handle)
        except CredentialError:
            raise
        except OSError:
            raise CredentialError('Key 没有保存成功。请重试，或取消“记住 Key”后仅本轮使用。') from None

    def forget(self):
        if not self.available:
            return
        try:
            with self._directory(private=False) as handle:
                self._unlink(NAME, handle)
                if handle is not None:
                    os.fsync(handle)
        except FileNotFoundError:
            pass
        except CredentialError:
            raise
        except OSError:
            raise CredentialError('暂时无法移除已保存的 Key，请重试。') from None
