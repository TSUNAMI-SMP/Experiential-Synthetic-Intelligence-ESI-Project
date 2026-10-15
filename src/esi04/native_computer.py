"""Real Windows SendInput/UI Automation and X11 XTest backends. No shell actions."""

import ctypes
import ctypes.util
import json
import ntpath
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from esi04.computer_models import Element, Foreground, Rect

KEYS = {"CTRL", "SHIFT", "ALT", "ENTER", "TAB", "ESC", "BACKSPACE", "DELETE", "HOME", "END",
        "LEFT", "RIGHT", "UP", "DOWN", "PAGEUP", "PAGEDOWN", "SPACE"} | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def validate_keys(keys):
    if not keys or len(keys) > 4 or any(k not in KEYS for k in keys):
        raise ValueError("利用できないキーの組み合わせです。")
    if len(keys) > 1 and any(k in keys for k in ("ALT",)) and any(k in keys for k in ("TAB", "ESC")):
        raise ValueError("アプリを切り替える操作は、切替先を観察してから行ってください。")


class WindowsBackend:
    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = w.HWND
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]

        class Mouse(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        class Keyboard(ctypes.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        class Union(ctypes.Union):
            _fields_ = [("mi", Mouse), ("ki", Keyboard)]
        class Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", w.DWORD), ("data", Union)]
        self.Mouse, self.Keyboard, self.Input = Mouse, Keyboard, Input
        self.user.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = w.UINT
        self.user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

    def foreground(self):
        w = self.w
        handle = self.user.GetForegroundWindow()
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(1001)
        self.user.GetWindowTextW(handle, title, len(title))
        rect = w.RECT()
        self.user.GetWindowRect(handle, ctypes.byref(rect))
        name, process = "", self.kernel.OpenProcess(0x1000, False, pid.value)
        if process:
            try:
                path = ctypes.create_unicode_buffer(32768)
                size = w.DWORD(len(path))
                if self.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                    name = ntpath.basename(path.value)
            finally:
                self.kernel.CloseHandle(process)
        bounds = Rect(x=rect.left, y=rect.top, width=max(1, rect.right-rect.left), height=max(1, rect.bottom-rect.top))
        return Foreground(handle=int(handle or 0), pid=pid.value, application=name or f"pid:{pid.value}",
                          title=title.value[:1000], bounds=bounds)

    def cursor(self):
        point = self.w.POINT()
        self.user.GetCursorPos(ctypes.byref(point))
        return [point.x, point.y]

    def elements(self, foreground):
        helper = Path(__file__).parent / "resources/uia.ps1"
        try:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(helper)], input=json.dumps({"handle": foreground.handle}),
                                    text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=3,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except (subprocess.TimeoutExpired, OSError):
            return []  # Inaccessible app trees use actual visual recognition instead.
        if result.returncode:
            return []
        try:
            raw = json.loads(result.stdout.lstrip("\ufeff"))
        except ValueError:
            return []
        return [Element.model_validate(item) for item in raw.get("elements", [])[:256]]

    def _send(self, inputs):
        array = (self.Input*len(inputs))(*inputs)
        if self.user.SendInput(len(array), array, ctypes.sizeof(self.Input)) != len(array):
            raise OSError("Windowsが入力を受け付けません。管理者画面などは操作できない場合があります。")

    def move(self, x, y):
        if not self.user.SetCursorPos(int(x), int(y)):
            raise OSError("カーソルを移動できません。")

    def click(self, x, y, double=False):
        self.move(x, y)
        for _ in range(2 if double else 1):
            self._send([self.Input(type=0, mi=self.Mouse(dwFlags=2)),
                        self.Input(type=0, mi=self.Mouse(dwFlags=4))])
            if double:
                time.sleep(0.08)

    def type(self, text):
        # UTF-16 code units support Japanese independently of the keyboard/IME layout.
        raw = text.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            scan = int.from_bytes(raw[i:i+2], "little")
            self._send([self.Input(type=1, ki=self.Keyboard(wScan=scan, dwFlags=4)),
                        self.Input(type=1, ki=self.Keyboard(wScan=scan, dwFlags=6))])

    def key(self, keys):
        validate_keys(keys)
        mapping = {"CTRL": 17, "SHIFT": 16, "ALT": 18, "ENTER": 13, "TAB": 9, "ESC": 27,
                   "BACKSPACE": 8, "DELETE": 46, "HOME": 36, "END": 35, "LEFT": 37, "UP": 38,
                   "RIGHT": 39, "DOWN": 40, "PAGEUP": 33, "PAGEDOWN": 34, "SPACE": 32}
        codes = [mapping.get(k, ord(k) if len(k) == 1 else 0) for k in keys]
        self._send([self.Input(type=1, ki=self.Keyboard(wVk=k)) for k in codes]
                   + [self.Input(type=1, ki=self.Keyboard(wVk=k, dwFlags=2)) for k in reversed(codes)])

    def scroll(self, amount):
        self._send([self.Input(type=0, mi=self.Mouse(mouseData=(amount*120) & 0xFFFFFFFF, dwFlags=0x800))])


class X11Backend:
    """Used for actual cloud desktop verification; normal Windows uses the class above."""

    def __init__(self):
        self.x = ctypes.CDLL(ctypes.util.find_library("X11"))
        self.t = ctypes.CDLL(ctypes.util.find_library("Xtst"))
        self.x.XInitThreads()
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise OSError("操作できるX11画面がありません。")
        ptr = ctypes.c_void_p
        self.x.XDefaultRootWindow.argtypes = [ptr]
        self.x.XDefaultRootWindow.restype = ctypes.c_ulong
        self.root = self.x.XDefaultRootWindow(self.display)
        self.x.XSync.argtypes = [ptr, ctypes.c_int]
        self.x.XGetInputFocus.argtypes = [ptr, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        self.x.XFetchName.argtypes = [ptr, ctypes.c_ulong, ctypes.POINTER(ctypes.c_char_p)]
        self.x.XFree.argtypes = [ptr]
        self.x.XQueryPointer.argtypes = [ptr, ctypes.c_ulong] + [ptr]*7
        self.x.XKeysymToKeycode.argtypes = [ptr, ctypes.c_ulong]
        self.x.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x.XStringToKeysym.restype = ctypes.c_ulong
        self.x.XGetKeyboardMapping.argtypes = [ptr, ctypes.c_ubyte, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.x.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        self.x.XChangeKeyboardMapping.argtypes = [ptr, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        self.t.XTestFakeMotionEvent.argtypes = [ptr, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        self.t.XTestFakeButtonEvent.argtypes = [ptr, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.t.XTestFakeKeyEvent.argtypes = [ptr, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.lock = threading.RLock()

    def foreground(self):
        window, revert = ctypes.c_ulong(), ctypes.c_int()
        self.x.XGetInputFocus(self.display, ctypes.byref(window), ctypes.byref(revert))
        name = ctypes.c_char_p()
        title = ""
        if window.value > 1 and self.x.XFetchName(self.display, window, ctypes.byref(name)) and name.value:
            title = name.value.decode("utf-8", "replace")
            self.x.XFree(name)
        return Foreground(handle=window.value, pid=0, application="X11:"+title.split(" — ")[0], title=title[:1000])

    def cursor(self):
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy, mask = [ctypes.c_int() for _ in range(5)]
        self.x.XQueryPointer(self.display, self.root, ctypes.byref(root), ctypes.byref(child),
                             ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
        return [rx.value, ry.value]

    def elements(self, foreground):
        return []  # X11 fallback uses actual visual anchors, not fabricated accessibility nodes.

    def move(self, x, y):
        with self.lock:
            self.t.XTestFakeMotionEvent(self.display, -1, int(x), int(y), 0)
            self.x.XSync(self.display, 0)

    def click(self, x, y, double=False):
        with self.lock:
            self.move(x, y)
            for _ in range(2 if double else 1):
                self.t.XTestFakeButtonEvent(self.display, 1, 1, 0)
                self.t.XTestFakeButtonEvent(self.display, 1, 0, 0)
                self.x.XSync(self.display, 0)
                if double:
                    time.sleep(0.08)

    def key(self, keys):
        validate_keys(keys)
        names = {"CTRL": "Control_L", "SHIFT": "Shift_L", "ALT": "Alt_L", "ENTER": "Return", "ESC": "Escape",
                 "SPACE": "space", "BACKSPACE": "BackSpace", "DELETE": "Delete", "PAGEUP": "Prior", "PAGEDOWN": "Next"}
        with self.lock:
            codes = [self.x.XKeysymToKeycode(self.display, self.x.XStringToKeysym(names.get(k, k.title() if len(k)>1 else k.lower()).encode()))
                     for k in keys]
            if not all(codes):
                raise ValueError("この画面のキーボードにないキーです。")
            for code in codes:
                self.t.XTestFakeKeyEvent(self.display, code, 1, 0)
            for code in reversed(codes):
                self.t.XTestFakeKeyEvent(self.display, code, 0, 0)
            self.x.XSync(self.display, 0)

    def type(self, text):
        with self.lock:
            for char in text:
                code = self.x.XKeysymToKeycode(self.display, ord(char)) if ord(char) < 256 else 0
                if not code:
                    self._unicode(char)
                    continue
                width = ctypes.c_int()
                symbols = self.x.XGetKeyboardMapping(self.display, code, 1, ctypes.byref(width))
                shifted = width.value > 1 and symbols[0] != ord(char) and symbols[1] == ord(char)
                self.x.XFree(symbols)
                shift = self.x.XKeysymToKeycode(self.display, 0xffe1)
                if shifted:
                    self.t.XTestFakeKeyEvent(self.display, shift, 1, 0)
                self.t.XTestFakeKeyEvent(self.display, code, 1, 0)
                self.t.XTestFakeKeyEvent(self.display, code, 0, 0)
                if shifted:
                    self.t.XTestFakeKeyEvent(self.display, shift, 0, 0)
                self.x.XSync(self.display, 0)

    def _unicode(self, text):
        with self.lock:
            # One temporary keycode avoids changing the clipboard or needing an external command.
            width = ctypes.c_int()
            previous = self.x.XGetKeyboardMapping(self.display, 248, 1, ctypes.byref(width))
            backup = (ctypes.c_ulong*width.value)(*[previous[i] for i in range(width.value)])
            self.x.XFree(previous)
            try:
                for char in text:
                    code = ord(char) if ord(char) < 256 else 0x01000000 | ord(char)
                    symbols = (ctypes.c_ulong*width.value)(code, *([0]*(width.value-1)))
                    self.x.XChangeKeyboardMapping(self.display, 248, width.value, symbols, 1)
                    self.x.XSync(self.display, 0)
                    self.t.XTestFakeKeyEvent(self.display, 248, 1, 0)
                    self.t.XTestFakeKeyEvent(self.display, 248, 0, 0)
                    self.x.XSync(self.display, 0)
                    time.sleep(0.035)
            finally:
                self.x.XChangeKeyboardMapping(self.display, 248, width.value, backup, 1)
                self.x.XSync(self.display, 0)

    def scroll(self, amount):
        with self.lock:
            for _ in range(abs(amount)):
                button = 4 if amount > 0 else 5
                self.t.XTestFakeButtonEvent(self.display, button, 1, 0)
                self.t.XTestFakeButtonEvent(self.display, button, 0, 0)
            self.x.XSync(self.display, 0)


def backend():
    return WindowsBackend() if os.name == "nt" else X11Backend()


class WindowsRecorder:
    """Low-level hooks only while demonstration is explicitly running; no raw typed text."""

    def __init__(self):
        self.events = queue.Queue(maxsize=512)
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="esi04-demonstration-hooks", daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            raise OSError(self.error or "操作実演の記録を開始できません。")

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x12, 0, 0)
        if self.thread:
            self.thread.join(timeout=3)
        self.thread = None

    def _run(self):
        from ctypes import wintypes as w
        user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
        self.thread_id = kernel.GetCurrentThreadId()
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
        user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, w.HINSTANCE, w.DWORD]
        user.SetWindowsHookExW.restype = w.HHOOK
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel.GetModuleHandleW.restype = w.HMODULE
        user.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        user.CallNextHookEx.restype = ctypes.c_ssize_t
        user.UnhookWindowsHookEx.argtypes = [w.HHOOK]

        class Mouse(ctypes.Structure):
            _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                        ("time", w.DWORD), ("extra", ctypes.c_size_t)]
        class Keyboard(ctypes.Structure):
            _fields_ = [("vk", w.DWORD), ("scan", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD),
                        ("extra", ctypes.c_size_t)]
        def emit(value):
            value["at"] = time.monotonic()
            try:
                self.events.put_nowait(value)
            except queue.Full:
                self.error = "実演の操作数が多すぎます。短い作業に分けてください。"
        def mouse(code, message, pointer):
            if code >= 0:
                event = ctypes.cast(pointer, ctypes.POINTER(Mouse)).contents
                if not event.flags & 1 and message == 0x201:
                    emit({"kind": "click", "x": event.pt.x, "y": event.pt.y})
                elif not event.flags & 1 and message == 0x20A:
                    emit({"kind": "scroll", "amount": ctypes.c_short(event.mouseData >> 16).value//120})
            return user.CallNextHookEx(None, code, message, pointer)
        def keyboard(code, message, pointer):
            if code >= 0 and message in {0x101, 0x105}:
                event = ctypes.cast(pointer, ctypes.POINTER(Keyboard)).contents
                if not event.flags & 0x10:
                    mapping = {13: "ENTER", 9: "TAB", 27: "ESC", 38: "UP", 40: "DOWN"}
                    if event.vk in mapping:
                        emit({"kind": "key", "keys": [mapping[event.vk]]})
                    elif user.GetAsyncKeyState(17) & 0x8000 and 65 <= event.vk <= 90:
                        emit({"kind": "key", "keys": ["CTRL", chr(event.vk)]})
                    elif event.vk not in {16, 17, 18, 91, 92}:
                        emit({"kind": "edit"})
            return user.CallNextHookEx(None, code, message, pointer)
        callbacks = callback_type(mouse), callback_type(keyboard)
        module = kernel.GetModuleHandleW(None)
        hooks = [user.SetWindowsHookExW(kind, callback, module, 0) for kind, callback in zip((14, 13), callbacks)]
        if not all(hooks):
            self.error = "Windowsが操作実演の記録を許可しませんでした。"
        self.ready.set()
        try:
            if all(hooks):
                message = w.MSG()
                while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    user.TranslateMessage(ctypes.byref(message))
                    user.DispatchMessageW(ctypes.byref(message))
        finally:
            for hook in hooks:
                if hook:
                    user.UnhookWindowsHookEx(hook)


class X11Recorder:
    def __init__(self):
        self.events = queue.Queue(maxsize=512)
        self.error = None
        self.thread = None
        self.context = None

    def start(self):
        from Xlib import X, display
        from Xlib.ext import record
        from Xlib.protocol import rq
        self.control = display.Display()
        self.listener = display.Display()
        if not self.listener.has_extension("RECORD"):
            raise OSError("このX11画面は実演の記録に対応していません。")
        self.context = self.listener.record_create_context(0, [record.AllClients], [{
            "core_requests": (0, 0), "core_replies": (0, 0), "ext_requests": (0, 0, 0, 0),
            "ext_replies": (0, 0, 0, 0), "delivered_events": (0, 0),
            "device_events": (X.KeyPress, X.ButtonPress), "errors": (0, 0),
            "client_started": False, "client_died": False}])
        def receive(reply):
            if reply.category != record.FromServer or reply.client_swapped or not reply.data:
                return
            data = reply.data
            while data:
                event, data = rq.EventField(None).parse_binary_value(data, self.listener.display, None, None)
                value = None
                if event.type == X.ButtonPress:
                    if event.detail == 1:
                        value = {"kind": "click", "x": event.root_x, "y": event.root_y}
                    elif event.detail in {4, 5}:
                        value = {"kind": "scroll", "amount": 1 if event.detail == 4 else -1}
                if value:
                    value["at"] = time.monotonic()
                    try:
                        self.events.put_nowait(value)
                    except queue.Full:
                        self.error = "実演の操作数が多すぎます。"
        self.thread = threading.Thread(target=self.listener.record_enable_context,
                                       args=(self.context, receive), daemon=True, name="esi04-x11-demonstration")
        self.thread.start()

    def stop(self):
        if self.context is not None:
            self.control.record_disable_context(self.context)
            self.control.flush()
            if self.thread:
                self.thread.join(timeout=3)
            self.listener.record_free_context(self.context)
            self.listener.close()
            self.control.close()
            self.context = None


def recorder():
    return WindowsRecorder() if os.name == "nt" else X11Recorder()
