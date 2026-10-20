# -*- coding: utf-8 -*-
"""Desktop UI Inspector 2.2.2 - Dense Modern UI, Windows / Python 3.9+.
Install: python -m pip install pywinauto Pillow pywin32
Run: python desktop_ui_inspector_v2_2_2_dense.py
No additional UI dependency. All automation objects stay in one MTA worker.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import io
import json
import keyword
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tokenize
import traceback
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog

APP = 'Desktop UI Inspector'
VERSION = '2.2.2'
IS_WINDOWS = sys.platform == 'win32'
DATA_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'DesktopUIInspector'
OUT_DIR = DATA_DIR / 'captures'
CONFIG = DATA_DIR / 'appearance.json'


def bind(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn


if IS_WINDOWS:
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    ole32 = ctypes.WinDLL('ole32', use_last_error=True)
    bind(user32, 'GetCursorPos', [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL)
    bind(user32, 'GetDesktopWindow', [], wintypes.HWND)
    bind(user32, 'GetAncestor', [wintypes.HWND, wintypes.UINT], wintypes.HWND)
    bind(user32, 'RegisterHotKey', [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL)
    bind(user32, 'UnregisterHotKey', [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    bind(user32, 'PeekMessageW', [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL)
    bind(user32, 'SetWindowPos', [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL)
    suffix = 'PtrW' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'W'
    get_style = bind(user32, 'GetWindowLong'+suffix, [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t)
    set_style = bind(user32, 'SetWindowLong'+suffix, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t)
    bind(kernel32, 'OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
    bind(kernel32, 'QueryFullProcessImageNameW', [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
    bind(kernel32, 'CloseHandle', [wintypes.HANDLE], wintypes.BOOL)
    bind(ole32, 'CoInitializeEx', [ctypes.c_void_p, wintypes.DWORD], ctypes.c_long)
    bind(ole32, 'CoUninitialize', [], None)


def cursor_pos():
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


def serial(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [serial(x) for x in value]
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    return str(value)


def read(fn, errors, name):
    try:
        return fn()
    except Exception as exc:
        errors[name] = f'{type(exc).__name__}: {exc}'
        return None


def info(wrapper, backend, full=False):
    result, errors = {'backend': backend}, {}
    element = wrapper.element_info
    names = ['name', 'class_name', 'handle', 'process_id']
    if backend == 'uia':
        names += ['automation_id', 'control_type']
    for name in names:
        result[name] = serial(read(lambda n=name: getattr(element, n), errors, name))
    if full:
        if backend == 'uia':
            for name in ('framework_id', 'runtime_id'):
                result[name] = serial(read(lambda n=name: getattr(element, n), errors, name))
        rect = read(lambda: element.rectangle, errors, 'rectangle')
        result['rectangle'] = {} if rect is None else dict(left=int(rect.left), top=int(rect.top), right=int(rect.right), bottom=int(rect.bottom), width=int(rect.right-rect.left), height=int(rect.bottom-rect.top))
        result['visible'] = read(wrapper.is_visible, errors, 'visible')
        result['enabled'] = read(wrapper.is_enabled, errors, 'enabled')
    result['read_errors'] = errors
    return result


def executable(pid):
    if not pid:
        return '', 'Process ID unavailable'
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return '', str(ctypes.WinError(ctypes.get_last_error()))
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return '', str(ctypes.WinError(ctypes.get_last_error()))
        return buffer.value, ''
    finally:
        kernel32.CloseHandle(handle)


def selector(data):
    keys = ('automation_id', 'control_type', 'class_name', 'name') if data['backend'] == 'uia' else ('class_name', 'name')
    return {key: data[key] for key in keys if data.get(key) not in (None, '')}


def generated_code(snapshot):
    path = snapshot.get('locator_path', snapshot.get('ancestors', []))
    top = snapshot.get('top_window', {})
    if not snapshot.get('path_complete'):
        return "raise RuntimeError('Incomplete parent path; refusing to generate a partial locator.')\n"
    if not path or not top.get('handle'):
        return "raise RuntimeError('Top-level window handle unavailable; capture again.')\n"
    lines = [
        'from pywinauto import Desktop', 'import time', '',
        '# HWND/PID identify this running session only. Re-capture after restart.',
        '# Each step searches DIRECT children. Ambiguity always raises an error.',
        'def direct_child(parent, criteria, step, timeout=10):',
        '    deadline = time.monotonic() + timeout',
        '    while True:',
        '        matches = []',
        '        for child in parent.children():',
        '            element = child.element_info',
        '            if all(getattr(element, key) == value for key, value in criteria.items()):',
        '                matches.append(child)',
        '        if len(matches) > 1:',
        "            raise RuntimeError(f'Step {step}: {len(matches)} direct children match {criteria!r}; narrow the selector.')",
        '        if len(matches) == 1:',
        '            return matches[0]',
        '        if time.monotonic() >= deadline:',
        "            raise RuntimeError(f'Step {step}: no direct child matches {criteria!r}; capture again if the UI changed.')",
        '        time.sleep(0.1)', '',
        f"desktop = Desktop(backend={snapshot['element']['backend']!r})",
        f"window = desktop.window(handle={top['handle']!r})",
        "window.wait('exists visible', timeout=10)",
        'node_0 = window.wrapper_object()',
        f"if node_0.element_info.process_id != {top.get('process_id')!r}:",
        "    raise RuntimeError('Top-window process changed; capture again.')",
    ]
    if top.get('class_name'):
        lines += [f"if node_0.element_info.class_name != {top['class_name']!r}:", "    raise RuntimeError('Top-window class changed; capture again.')"]
    for index, item in enumerate(path[1:], 1):
        criteria = selector(item)
        if not criteria:
            lines += [f"raise RuntimeError('Step {index}: no usable selector; refusing to skip this parent.')"]
            return '\n'.join(lines)+'\n'
        label = str(item.get('name') or item.get('automation_id') or item.get('class_name') or '<unnamed>')
        label = label.replace('\n', ' ').replace('\r', ' ')
        lines += ['', f'# Step {index}: {label}', f'node_{index} = direct_child(node_{index-1}, {criteria!r}, step={index})']
    lines += ['', f'target = node_{len(path)-1}',
              'if not target.is_visible() or not target.is_enabled():',
              "    raise RuntimeError('Target is not visible/enabled.')", 'print(target.element_info)', '',
              '# Inspection only. Review the target before enabling an action.',
              '# target.set_edit_text("...")', '# target.click_input()']
    return '\n'.join(lines)+'\n'


class Cancelled(Exception):
    pass


class Worker(threading.Thread):
    def __init__(self, events):
        super().__init__(name='Inspector-MTA', daemon=True)
        self.events, self.commands = events, queue.Queue()
        self.cancel, self.stopping = threading.Event(), threading.Event()
        self.current, self.chain, self.backend = None, [], None

    def check(self, deadline=None):
        if self.cancel.is_set() or self.stopping.is_set():
            raise Cancelled('操作已取消（底层调用返回后生效）。')
        if deadline is not None and time.monotonic() >= deadline:
            raise Cancelled('达到协作式时间预算；不是强制超时。')

    def run(self):
        initialized = False
        try:
            hr = ole32.CoInitializeEx(None, 0)
            if hr not in (0, 1):
                raise RuntimeError(f'Cannot initialize MTA: 0x{hr & 0xffffffff:08X}')
            initialized = True
            from pywinauto import Desktop
            self.Desktop = Desktop
            self.events.put(('ready', None, None))
            while not self.stopping.is_set():
                command = self.commands.get()
                if command is None:
                    break
                ident, operation, payload = command
                start = time.monotonic()
                try:
                    self.check()
                    result = self.execute(operation, payload)
                    self.events.put(('result', ident, (operation, result, time.monotonic()-start)))
                except Exception as exc:
                    self.events.put(('error', ident, (str(exc), traceback.format_exc())))
                finally:
                    self.events.put(('done', ident, None))
        except Exception:
            self.events.put(('fatal', None, traceback.format_exc()))
        finally:
            self.current = None
            self.chain.clear()
            if initialized:
                ole32.CoUninitialize()

    def execute(self, operation, payload):
        if operation == 'capture':
            backend = payload['backend']
            wrapper = self.Desktop(backend=backend).from_point(*payload['point'])
            if wrapper.element_info.process_id == os.getpid():
                raise RuntimeError('捕获到了 Inspector 自身，请把鼠标移到目标应用。')
            return self.capture(wrapper, backend, payload['point'])
        if self.current is None:
            raise RuntimeError('请先捕获一个控件。')
        if operation == 'parent':
            if len(self.chain) <= 1:
                raise RuntimeError('已经位于应用顶层窗口。')
            return self.capture(self.chain[-2], self.backend)
        if operation == 'select':
            return self.capture(self.chain[payload['index']], self.backend)
        if operation == 'properties':
            data = info(self.current, self.backend, True)
            for key in ('texts', 'window_text', 'friendly_class_name'):
                data[key] = serial(read(lambda k=key: getattr(self.current, k)(), data['read_errors'], key))
            return data
        if operation == 'tree':
            root = self.current if payload['scope'] == 'captured' else self.chain[0]
            return self.tree(root, payload)
        raise ValueError(operation)

    def capture(self, wrapper, backend, point=None):
        top = wrapper.top_level_parent()
        if top.element_info.handle == user32.GetDesktopWindow():
            raise RuntimeError('不允许从整个桌面读取。')
        chain, path, warnings = [], [], []
        current, complete = wrapper, False
        for _ in range(128):
            self.check()
            if current is None or current.element_info.handle == user32.GetDesktopWindow():
                break
            if any(current == previous for previous in chain):
                warnings.append('Ancestor cycle detected.')
                break
            chain.append(current)
            path.append(info(current, backend))
            if current == top:
                complete = True
                break
            current = current.parent()
        if not complete:
            warnings.append('Incomplete parent path; code generation disabled.')
        chain.reverse()
        path.reverse()
        data = info(wrapper, backend, True)
        data['captured_at'] = datetime.now().isoformat(timespec='seconds')
        if point is not None:
            data['cursor'] = dict(x=point[0], y=point[1])
        data['executable'], error = executable(data.get('process_id'))
        if error:
            data['read_errors']['executable'] = error
        snapshot = dict(element=data, top_window=info(top, backend), ancestors=path,
                        locator_path=path, path_complete=complete,
                        is_top_window=wrapper == top, warnings=warnings)
        self.check()
        self.current, self.chain, self.backend = wrapper, chain, backend
        return snapshot

    def tree(self, root, options):
        deadline = time.monotonic()+options['seconds']
        lines, errors, count, incomplete = [], [], 0, False
        def walk(node, depth):
            nonlocal count, incomplete
            self.check(deadline)
            if count >= options['nodes']:
                incomplete = True
                return
            data = info(node, self.backend)
            count += 1
            label = str(data.get('name') or '<unnamed>').replace('\n', ' ').replace('\r', ' ')
            lines.append('    '*depth+f"[{data.get('control_type') or data.get('class_name')}] {label} (auto_id={data.get('automation_id')!r}, class={data.get('class_name')!r})")
            if data['read_errors']:
                errors.append(data['read_errors'])
            if depth >= options['depth']:
                incomplete = True
                return
            self.check(deadline)
            try:
                children = node.children()
            except Exception as exc:
                errors.append(str(exc))
                return
            self.check(deadline)
            for child in children:
                if count >= options['nodes']:
                    incomplete = True
                    break
                walk(child, depth+1)
        try:
            walk(root, 0)
        except Cancelled as exc:
            incomplete = True
            errors.append(str(exc))
        incomplete = incomplete or bool(errors)
        header = f'Nodes: {count}; incomplete/boundary reached: {incomplete}'
        if errors:
            header += '\nDiagnostics: '+json.dumps(errors, ensure_ascii=False)
        return header+'\n\n'+'\n'.join(lines)

    def stop(self):
        self.stopping.set()
        self.cancel.set()
        self.commands.put(None)


class Hotkeys(threading.Thread):
    def __init__(self, events):
        super().__init__(name='Hotkeys', daemon=True)
        self.events, self.stopping = events, threading.Event()

    def run(self):
        registered = []
        mapping = {1001:'capture', 1002:'parent', 1003:'pause'}
        try:
            for ident, key in ((1001,0x77), (1002,0x78), (1003,0x79)):
                if user32.RegisterHotKey(None, ident, 0x4006, key):
                    registered.append(ident)
                else:
                    self.events.put(('notice', None, f'Hotkey {ident}: {ctypes.WinError(ctypes.get_last_error())}'))
            message = wintypes.MSG()
            while not self.stopping.is_set():
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == 0x0312 and message.wParam in mapping:
                        action = mapping[message.wParam]
                        self.events.put(('hotkey', None, (action, cursor_pos() if action == 'capture' else None)))
                self.stopping.wait(0.03)
        except Exception:
            self.events.put(('notice', None, traceback.format_exc()))
        finally:
            for ident in registered:
                user32.UnregisterHotKey(None, ident)


PALETTES = {
    'light': dict(bg='#F3F6FB', card='#FFFFFF', text='#18283E', muted='#65758B', border='#DFE6F0',
                  accent='#3868ED', hover='#2854CE', soft='#EAF0FF', selected='#DCE7FF',
                  editor='#FAFCFF', green='#16836D', red='#CC4558', code='#30445E'),
    'dark': dict(bg='#111827', card='#1B2536', text='#E5EDF9', muted='#9CAEC6', border='#303E54',
                 accent='#6C96FF', hover='#88AAFF', soft='#273A60', selected='#304D7E',
                 editor='#151E2E', green='#5DD5B1', red='#FF8496', code='#CBD9F0'),
}
TAB_NAMES = {'Summary':'概览', 'All Properties':'详细属性', 'Generated Code':'定位代码', 'Control Tree':'控件树', 'Log':'运行日志'}
OP_NAMES = {'capture':'捕获控件', 'parent':'选择父级', 'select':'切换控件', 'properties':'读取属性', 'tree':'读取控件树'}


class SlimScrollbar(tk.Canvas):
    """Arrow-free scrollbar. Supports dragging, track clicks and auto-hide.
    Widget uses grid in the inspector; hidden bars restore their grid options.
    """
    def __init__(self, parent, command=None, orient='vertical', **kwargs):
        self.horizontal = orient == 'horizontal'
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.drag_start = None
        self.hovered = False
        self.colors = dict(track='#FAFCFF', thumb='#CAD4E3', hover='#98AAC3')
        super().__init__(parent, width=9, height=9, bd=0,
                         highlightthickness=0, takefocus=False, **kwargs)
        self.bind('<Configure>', lambda e: self.redraw())
        self.bind('<Enter>', self.enter)
        self.bind('<Leave>', self.leave)
        self.bind('<Button-1>', self.press)
        self.bind('<B1-Motion>', self.drag)
        self.bind('<ButtonRelease-1>', self.release)
        self.bind('<MouseWheel>', self.wheel)
        self.bind('<Button-4>', lambda e: self.scroll(-3))
        self.bind('<Button-5>', lambda e: self.scroll(3))

    def set_theme(self, colors):
        self.colors = dict(track=colors['editor'], thumb=colors['border'], hover=colors['muted'])
        self.configure(bg=self.colors['track'])
        self.redraw()

    def set(self, first, last):
        self.first = max(0.0, min(1.0, float(first)))
        self.last = max(self.first, min(1.0, float(last)))
        if self.first <= 0 and self.last >= 1:
            self.grid_remove()
        else:
            self.grid()
        self.redraw()

    def metrics(self):
        length = max(1, self.winfo_width() if self.horizontal else self.winfo_height())
        thumb = min(length, max(24, (self.last-self.first)*length))
        travel = max(0, length-thumb)
        fraction = max(0.0, 1.0-(self.last-self.first))
        start = (self.first/fraction)*travel if fraction else 0
        return length, thumb, travel, start

    def redraw(self):
        self.delete('all')
        length, thumb, travel, start = self.metrics()
        color = self.colors['hover'] if self.hovered or self.drag_start else self.colors['thumb']
        # A round-ended line gives a clean thumb without native arrow buttons.
        if self.horizontal:
            y = self.winfo_height()/2
            self.create_line(start+3, y, start+thumb-3, y, fill=color, width=5, capstyle='round')
        else:
            x = self.winfo_width()/2
            self.create_line(x, start+3, x, start+thumb-3, fill=color, width=5, capstyle='round')

    def enter(self, event):
        self.hovered = True
        self.redraw()

    def leave(self, event):
        self.hovered = False
        self.redraw()

    def press(self, event):
        pos = event.x if self.horizontal else event.y
        length, thumb, travel, start = self.metrics()
        if start <= pos <= start+thumb:
            self.drag_start = (pos, self.first)
        else:
            fraction = max(0.0, 1.0-(self.last-self.first))
            value = max(0.0, min(fraction, ((pos-thumb/2)/travel)*fraction)) if travel else 0.0
            if self.command:
                self.command('moveto', value)
            self.drag_start = (pos, value)
        self.redraw()

    def drag(self, event):
        if self.drag_start is None or not self.command:
            return
        pos = event.x if self.horizontal else event.y
        length, thumb, travel, start = self.metrics()
        fraction = max(0.0, 1.0-(self.last-self.first))
        value = self.drag_start[1]+((pos-self.drag_start[0])/travel)*fraction if travel else 0.0
        self.command('moveto', max(0.0, min(fraction, value)))

    def release(self, event):
        self.drag_start = None
        self.redraw()

    def scroll(self, units):
        if self.command:
            self.command('scroll', units, 'units')
        return 'break'

    def wheel(self, event):
        return self.scroll(-max(1, abs(int(event.delta/120)))*3 if event.delta > 0 else max(1, abs(int(event.delta/120)))*3)


class App(tk.Tk):
    def __init__(self, preview=False):
        super().__init__()
        self.preview = preview
        self.title(f'{APP}  {VERSION}')
        # Keep the initial window inside common laptop displays.
        width = min(1380, max(900, self.winfo_screenwidth()-70))
        height = min(900, max(620, self.winfo_screenheight()-90))
        self.geometry(f'{width}x{height}')
        self.minsize(min(1040, width), min(680, height))
        self.events = queue.Queue()
        self.snapshot = None
        self.busy = self.ready = self.closing = self.paused = False
        self.ident, self.active = 0, None
        self.timer = self.poll_timer = None
        self.overlays, self.color_widgets, self.buttons = [], [], []
        self.scrollbars = []
        self.tab_buttons, self.tab_frames, self.tabs = {}, {}, {}
        self.current_tab = 'Summary'
        self.theme = 'light'
        try:
            saved = json.loads(CONFIG.read_text(encoding='utf-8'))
            if isinstance(saved, dict) and saved.get('theme') in PALETTES:
                self.theme = saved['theme']
        except (OSError, ValueError):
            pass
        self.backend = tk.StringVar(value='uia')
        self.scope = tk.StringVar(value='captured')
        self.depth, self.nodes, self.seconds = tk.StringVar(value='4'), tk.StringVar(value='300'), tk.StringVar(value='15')
        self.status = tk.StringVar(value='正在初始化自动化引擎…')
        self.target_title = tk.StringVar(value='尚未捕获控件')
        self.target_detail = tk.StringVar(value='将鼠标移到目标应用，按 Ctrl + Shift + F8 开始')
        self.path_badge = tk.StringVar(value='等待捕获')
        self.engine_badge = tk.StringVar(value='初始化')
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.option_add('*Font', ('Microsoft YaHei UI' if IS_WINDOWS else 'DejaVu Sans', 10))
        self.build()
        self.apply_theme()
        self.worker = Worker(self.events)
        self.hotkeys = Hotkeys(self.events)
        if not preview:
            self.worker.start()
            self.hotkeys.start()
        else:
            self.ready = True
            self.engine_badge.set('界面预览')
            self.status.set('界面预览模式：未连接 Windows 自动化引擎。')
        self.sync_actions()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_timer = self.after(60, self.poll)

    def frame(self, parent, role='card', **kwargs):
        widget = tk.Frame(parent, bd=0, **kwargs)
        self.color_widgets.append((widget, 'frame', role))
        return widget

    def label(self, parent, text=None, variable=None, role='text', bg='card', **kwargs):
        widget = tk.Label(parent, text=text, textvariable=variable, bd=0, anchor='w', **kwargs)
        self.color_widgets.append((widget, 'label', (role, bg)))
        return widget

    def button(self, parent, text, command, kind='normal', action='always'):
        widget = tk.Button(parent, text=text, command=command, bd=0, relief='flat',
                           padx=10, pady=4, cursor='hand2', takefocus=True, highlightthickness=1)
        self.buttons.append((widget, kind, action))
        return widget

    def build(self):
        # Reserve only the central workspace as expandable. Toolbars stay compact.
        outer = self.frame(self, 'bg')
        outer.pack(fill='both', expand=True, padx=10, pady=8)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = self.frame(outer, 'bg')
        header.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        header.columnconfigure(1, weight=1)
        self.label(header, APP, bg='bg', font=('Segoe UI', 14, 'bold')).grid(row=0, column=0, sticky='w')
        self.label(header, 'V'+VERSION, role='muted', bg='bg').grid(row=0, column=1, sticky='w', padx=10)
        self.label(header, variable=self.engine_badge, role='green', bg='bg').grid(row=0, column=2, padx=10)
        self.label(header, '引擎', role='muted', bg='bg').grid(row=0, column=3, padx=(8, 4))
        self.backend_box = ttk.Combobox(header, textvariable=self.backend, values=('uia', 'win32'), state='readonly', width=7)
        self.backend_box.grid(row=0, column=4, padx=(0, 8))
        self.backend_box.bind('<<ComboboxSelected>>', self.backend_changed)
        self.theme_button = self.button(header, '切换深色', self.toggle_theme)
        self.theme_button.grid(row=0, column=5)

        tools = self.frame(outer)
        tools.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        # Content-sized controls: no uniform columns and no horizontal stretching.
        actions = [
            ('捕获 · 3 秒', self.delayed, 'primary', 'capture'),
            ('上一级', lambda: self.request('parent'), 'normal', 'target'),
            ('高亮', self.highlight, 'normal', 'target'),
            ('详细属性', lambda: self.request('properties'), 'normal', 'target'),
            ('保存 JSON', self.save_json, 'normal', 'target'),
            ('截图', self.screenshot, 'normal', 'target'),
            ('复制代码', self.copy_code, 'normal', 'target'),
            ('取消', self.cancel, 'danger', 'cancel'),
        ]
        self.toolbar_buttons = []
        for col, (title, callback, kind, action) in enumerate(actions):
            button = self.button(tools, title, callback, kind, action)
            button.grid(row=0, column=col, sticky='w', padx=(0 if col == 0 else 4, 0), pady=3)
            self.toolbar_buttons.append(button)

        target = self.frame(outer)
        target.grid(row=2, column=0, sticky='ew', pady=(0, 6))
        target.columnconfigure(0, weight=1)
        self.target_name_label = self.label(target, variable=self.target_title, font=('Microsoft YaHei UI', 10, 'bold'))
        self.target_name_label.grid(row=0, column=0, sticky='w', padx=10, pady=(5, 0))
        self.label(target, variable=self.path_badge, role='accent').grid(row=0, column=1, padx=10)
        self.target_detail_label = self.label(target, variable=self.target_detail, role='muted', font=('Microsoft YaHei UI', 9))
        self.target_detail_label.grid(row=1, column=0, columnspan=2, sticky='w', padx=10, pady=(0, 5))
        # Limit the display only, not the exported data or selectors.
        def fit_target(event):
            width = max(180, event.width-180)
            self.target_name_label.configure(wraplength=width)
            self.target_detail_label.configure(wraplength=max(180, event.width-24))
        target.bind('<Configure>', fit_target)

        self.pane = ttk.Panedwindow(outer, orient='horizontal')
        self.pane.grid(row=3, column=0, sticky='nsew')
        left, right = self.frame(self.pane), self.frame(self.pane)
        self.pane.add(left, weight=4)
        self.pane.add(right, weight=6)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        tree_header = self.frame(left)
        tree_header.grid(row=0, column=0, sticky='ew', padx=8, pady=6)
        self.label(tree_header, '控件层级', font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left')
        self.label(tree_header, '双击选择父级', role='muted', font=('Microsoft YaHei UI', 9)).pack(side='right')
        holder = self.frame(left)
        holder.grid(row=1, column=0, sticky='nsew', padx=6)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.hierarchy = ttk.Treeview(holder, columns=('type', 'id', 'class'), show='tree headings', selectmode='browse')
        for col, title, width in [('#0', '名称', 160), ('type', '类型', 75), ('id', 'Automation ID', 150), ('class', 'Class', 100)]:
            self.hierarchy.heading(col, text=title)
            self.hierarchy.column(col, width=width, minwidth=55, stretch=False)
        sy = SlimScrollbar(holder, command=self.hierarchy.yview)
        sx = SlimScrollbar(holder, orient='horizontal', command=self.hierarchy.xview)
        self.scrollbars.extend((sy, sx))
        self.hierarchy.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.hierarchy.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        self.hierarchy.bind('<Double-1>', self.select_parent)

        # Compact form: one scope row and one bounded-parameter row.
        settings = self.frame(left)
        settings.grid(row=2, column=0, sticky='ew', padx=8, pady=6)
        scope_row = self.frame(settings)
        scope_row.grid(row=0, column=0, sticky='w')
        self.label(scope_row, '范围', role='muted').grid(row=0, column=0, padx=(0, 6))
        self.scope_box = ttk.Combobox(scope_row, textvariable=self.scope,
                                     values=('captured', 'top window'), state='readonly', width=12)
        self.scope_box.grid(row=0, column=1)
        self.button(scope_row, '读取树', self.read_tree, action='target').grid(row=0, column=2, padx=(8, 0))
        limits = self.frame(settings)
        limits.grid(row=1, column=0, sticky='w', pady=(5, 0))
        for col, (title, var, low, high, width) in enumerate([
                ('深度', self.depth, 0, 20, 3),
                ('节点', self.nodes, 1, 10000, 6),
                ('预算/秒', self.seconds, 1, 120, 4)]):
            self.label(limits, title, role='muted').grid(row=0, column=col*2, padx=(0 if col == 0 else 10, 5))
            ttk.Spinbox(limits, textvariable=var, from_=low, to=high, width=width).grid(row=0, column=col*2+1)
        self.label(settings, '协作式取消；底层调用返回后生效', role='muted', font=('Microsoft YaHei UI', 8)).grid(row=2, column=0, sticky='w', pady=(4, 0))

        tabbar = self.frame(right)
        tabbar.grid(row=0, column=0, sticky='ew', padx=6, pady=(6, 0))
        for col, (key, title) in enumerate(TAB_NAMES.items()):
            button = self.button(tabbar, title, lambda name=key: self.switch_tab(name))
            button.configure(padx=12, pady=4)
            button.grid(row=0, column=col, sticky='w', padx=(0, 3))
            self.tab_buttons[key] = button
        self.tab_hint = tk.StringVar(value='')
        self.hint_label = self.label(right, variable=self.tab_hint, role='muted', font=('Microsoft YaHei UI', 8))
        self.hint_label.grid(row=1, column=0, sticky='w', padx=10, pady=4)
        right.bind('<Configure>', lambda e: self.hint_label.configure(wraplength=max(180, e.width-24)))
        content = self.frame(right)
        content.grid(row=2, column=0, sticky='nsew', padx=6, pady=(0, 6))
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        for key in TAB_NAMES:
            frame = self.frame(content)
            frame.grid(row=0, column=0, sticky='nsew')
            text = tk.Text(frame, wrap='none', font=('Consolas', 10), bd=0, highlightthickness=0,
                           padx=8, pady=6, undo=False, state='disabled', takefocus=True)
            sy = SlimScrollbar(frame, command=text.yview)
            sx = SlimScrollbar(frame, orient='horizontal', command=text.xview)
            self.scrollbars.extend((sy, sx))
            text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
            text.grid(row=0, column=0, sticky='nsew')
            sy.grid(row=0, column=1, sticky='ns')
            sx.grid(row=1, column=0, sticky='ew')
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            text.bind('<Control-a>', lambda event, w=text: self.select_all(w))
            self.tab_frames[key], self.tabs[key] = frame, text
        for name in TAB_NAMES:
            self.text(name, '尚无数据。\n\n鼠标移到目标控件，按 Ctrl + Shift + F8 捕获。')
        self.switch_tab('Summary')

        footer = self.frame(outer, 'bg')
        footer.grid(row=4, column=0, sticky='ew', pady=(5, 0))
        footer.columnconfigure(1, weight=1)
        self.status_dot = self.label(footer, '●', role='green', bg='bg', font=('Segoe UI', 10))
        self.status_dot.grid(row=0, column=0, padx=(0, 5))
        self.status_label = self.label(footer, variable=self.status, bg='bg', font=('Microsoft YaHei UI', 8))
        self.status_label.grid(row=0, column=1, sticky='w')
        self.label(footer, 'Ctrl+Shift：F8 捕获 / F9 父级 / F10 暂停', role='muted', bg='bg', font=('Microsoft YaHei UI', 8)).grid(row=0, column=2, padx=8)
        self.progress = ttk.Progressbar(footer, mode='indeterminate', length=65)
        self.progress.grid(row=0, column=3)
        footer.bind('<Configure>', lambda e: self.status_label.configure(wraplength=max(150, e.width-420)))
        self.after_idle(self.initial_sash)

    def initial_sash(self):
        try:
            self.pane.sashpos(0,int(self.pane.winfo_width()*0.40))
        except tk.TclError:
            pass

    def apply_theme(self):
        c=PALETTES[self.theme]
        self.configure(bg=c['bg'])
        self.style.configure('.',font=('Microsoft YaHei UI' if IS_WINDOWS else 'DejaVu Sans',10),background=c['card'],foreground=c['text'])
        self.style.configure('TPanedwindow',background=c['bg'],sashwidth=10)
        self.style.configure('Treeview',background=c['card'],fieldbackground=c['card'],foreground=c['text'],rowheight=27,borderwidth=0,relief='flat')
        self.style.map('Treeview',background=[('selected',c['selected'])],foreground=[('selected',c['text'])])
        self.style.configure('Treeview.Heading',background=c['editor'],foreground=c['muted'],font=('Microsoft YaHei UI' if IS_WINDOWS else 'DejaVu Sans',9,'bold'),padding=(6,6),relief='flat')
        self.style.map('Treeview.Heading',background=[('active',c['soft'])])
        for style in ('TEntry','TCombobox','TSpinbox'):
            self.style.configure(style,fieldbackground=c['editor'],background=c['card'],foreground=c['text'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],insertcolor=c['text'],padding=5)
            self.style.map(style,fieldbackground=[('readonly',c['editor']),('disabled',c['bg'])],foreground=[('disabled',c['muted']),('readonly',c['text'])],selectbackground=[('readonly',c['selected'])],selectforeground=[('readonly',c['text'])])
        self.style.configure('TScrollbar',background=c['border'],troughcolor=c['card'],bordercolor=c['card'],arrowcolor=c['muted'],relief='flat',arrowsize=12)
        self.style.configure('Horizontal.TProgressbar',background=c['accent'],troughcolor=c['border'],borderwidth=0)
        self.option_add('*TCombobox*Listbox.background',c['card'])
        self.option_add('*TCombobox*Listbox.foreground',c['text'])
        self.option_add('*TCombobox*Listbox.selectBackground',c['selected'])
        self.option_add('*TCombobox*Listbox.selectForeground',c['text'])
        for widget,kind,role in self.color_widgets:
            if kind=='frame':
                widget.configure(bg=c[role])
            else:
                fg,bg=role
                widget.configure(bg=c[bg],fg=c[fg])
        for widget,kind,action in self.buttons:
            if kind=='primary':
                bg,fg,hover=c['accent'],('#FFFFFF' if self.theme=='light' else '#111827'),c['hover']
            elif kind=='danger':
                bg,fg,hover=c['card'],c['red'],c['soft']
            else:
                bg,fg,hover=c['editor'],c['text'],c['soft']
            widget.configure(bg=bg,fg=fg,activebackground=hover,activeforeground=fg,
                             disabledforeground=(('#DDE7FF' if self.theme=='light' else '#273A60') if kind=='primary' else c['muted']),highlightbackground=c['border'],highlightcolor=c['accent'])
        for text in self.tabs.values():
            text.configure(bg=c['editor'],fg=c['code'],insertbackground=c['text'],selectbackground=c['selected'],selectforeground=c['text'])
            text.tag_configure('string',foreground='#1D826A' if self.theme=='light' else '#85D5AE')
            text.tag_configure('number',foreground='#AF6530' if self.theme=='light' else '#F2BA85')
            text.tag_configure('keyword',foreground='#7950B8' if self.theme=='light' else '#C5A4FF')
            text.tag_configure('comment',foreground=c['muted'])
        for scrollbar in self.scrollbars:
            scrollbar.set_theme(c)
        self.theme_button.configure(text='切换深色' if self.theme=='light' else '切换浅色')
        self.style_tabs()

    def toggle_theme(self):
        self.theme='dark' if self.theme=='light' else 'light'
        self.apply_theme()
        self.sync_actions()
        try:
            DATA_DIR.mkdir(parents=True,exist_ok=True)
            CONFIG.write_text(json.dumps({'theme':self.theme}),encoding='utf-8')
        except OSError as exc:
            self.log(f'外观设置未保存：{exc}')

    def style_tabs(self):
        c=PALETTES[self.theme]
        for name,button in self.tab_buttons.items():
            button.configure(bg=c['soft'] if name==self.current_tab else c['card'],
                             fg=c['accent'] if name==self.current_tab else c['muted'],
                             highlightbackground=c['soft'] if name==self.current_tab else c['card'])

    def switch_tab(self,name):
        self.current_tab=name
        self.tab_frames[name].tkraise()
        hints={'Summary':'原始结构化信息，可通过“保存 JSON”导出',
               'All Properties':'按需读取详细属性；读取后会一并保存到 JSON',
               'Generated Code':'完整父级路径 · 逐层唯一匹配 · 默认不执行点击',
               'Control Tree':'按左侧范围向下读取；此处文本不包含在 JSON 中',
               'Log':'操作耗时与错误诊断；日志需单独复制'}
        self.tab_hint.set(hints[name])
        self.style_tabs()

    def select_all(self,widget):
        widget.tag_add('sel','1.0','end-1c')
        return 'break'

    def text(self,name,value):
        widget=self.tabs[name]
        widget.configure(state='normal')
        widget.delete('1.0','end')
        widget.insert('1.0',value)
        self.colorize(widget,value)
        widget.configure(state='disabled')

    def colorize(self,widget,value):
        if len(value)>180000:
            return
        try:
            for token in tokenize.generate_tokens(io.StringIO(value).readline):
                tag=None
                if token.type==tokenize.STRING: tag='string'
                elif token.type==tokenize.NUMBER: tag='number'
                elif token.type==tokenize.COMMENT: tag='comment'
                elif token.type==tokenize.NAME and (keyword.iskeyword(token.string) or token.string in ('true','false','null')): tag='keyword'
                if tag:
                    widget.tag_add(tag,f'{token.start[0]}.{token.start[1]}',f'{token.end[0]}.{token.end[1]}')
        except (tokenize.TokenError,IndentationError,SyntaxError):
            pass

    def log(self,value):
        widget=self.tabs['Log']
        widget.configure(state='normal')
        widget.insert('end',f'[{datetime.now():%H:%M:%S}] {value}\n')
        if int(widget.index('end-1c').split('.')[0])>3000:
            widget.delete('1.0','501.0')
        widget.see('end')
        widget.configure(state='disabled')

    def sync_actions(self):
        for widget,kind,action in self.buttons:
            enabled=True
            if action=='capture': enabled=self.ready and not self.busy
            elif action=='target': enabled=self.snapshot is not None and not self.busy
            elif action=='cancel': enabled=self.busy or self.timer is not None
            widget.configure(state='normal' if enabled else 'disabled')
        self.backend_box.configure(state='disabled' if self.busy else 'readonly')
        self.engine_badge.set('界面预览' if self.preview else ('处理中' if self.busy else ('引擎就绪' if self.ready else '引擎未就绪')))
        self.status_dot.configure(fg=PALETTES[self.theme]['accent' if self.busy else ('green' if self.ready else 'red')])
        if self.busy:
            self.progress.start(14)
        else:
            self.progress.stop()
            self.progress['value']=0

    def report_callback_exception(self,kind,value,tb):
        self.status.set(str(value))
        self.log(''.join(traceback.format_exception(kind,value,tb)))
        self.switch_tab('Log')

    def poll(self):
        try:
            for _ in range(100):
                try: kind,ident,value=self.events.get_nowait()
                except queue.Empty: break
                try: self.event(kind,ident,value)
                except Exception:
                    self.status.set('界面更新失败，请查看运行日志。')
                    self.log(traceback.format_exc())
        finally:
            if not self.closing:
                self.poll_timer=self.after(60,self.poll)

    def event(self,kind,ident,value):
        if kind=='ready':
            self.ready=True
            self.status.set('准备就绪 · 将鼠标移到目标控件，按 Ctrl + Shift + F8 捕获')
            self.sync_actions()
        elif kind in ('fatal','notice'):
            if kind=='fatal': self.ready=False
            self.status.set(value.splitlines()[-1])
            self.log(value)
            self.sync_actions()
        elif kind=='hotkey':
            action,point=value
            if action=='pause':
                self.paused=not self.paused
                self.status.set('全局快捷键已暂停。' if self.paused else '全局快捷键已恢复。')
            elif not self.paused:
                if action=='capture': self.capture(point)
                else: self.request('parent')
        elif ident==self.active:
            if kind=='result':
                operation,result,elapsed=value
                if operation in ('capture','parent','select'): self.show(result)
                elif operation=='properties':
                    self.snapshot['properties']=result
                    if result.get('read_errors'):
                        self.log('详细属性读取存在错误，目标控件可能已变化；请重新捕获后再读取。')
                    self.text('All Properties',json.dumps(result,ensure_ascii=False,indent=2))
                    self.text('Summary',json.dumps(self.snapshot,ensure_ascii=False,indent=2))
                    self.switch_tab('All Properties')
                elif operation=='tree':
                    self.text('Control Tree',result)
                    self.switch_tab('Control Tree')
                self.status.set(f'{OP_NAMES[operation]}完成 · {elapsed:.2f} 秒')
                self.log(self.status.get())
            elif kind=='error':
                self.status.set(value[0])
                self.log(value[1])
                self.switch_tab('Log')
            elif kind=='done':
                self.busy,self.active=False,None
                self.sync_actions()

    def request(self,operation,payload=None):
        if self.preview:
            self.status.set('界面预览模式不执行 Windows 自动化。')
            return
        if not self.ready or self.busy:
            self.status.set('引擎未就绪或正在处理其他操作。')
            return
        if operation!='capture' and self.snapshot is None:
            self.status.set('请先捕获一个控件。')
            return
        self.stop_timer()
        self.ident+=1
        self.active,self.busy=self.ident,True
        self.worker.cancel.clear()
        self.status.set(OP_NAMES[operation]+'中…')
        self.sync_actions()
        self.worker.commands.put((self.active,operation,payload or {}))

    def show(self,snapshot):
        self.snapshot=snapshot
        self.text('Summary',json.dumps(snapshot,ensure_ascii=False,indent=2))
        self.text('All Properties','点击“读取详细属性”获取文本及其他属性。')
        self.text('Generated Code',generated_code(snapshot))
        self.text('Control Tree','在左侧选择范围，然后点击“读取控件树”。')
        data=snapshot['element']
        self.target_title.set(str(data.get('name') or data.get('automation_id') or '<未命名控件>')[:110])
        self.target_detail.set(f"类型 {data.get('control_type') or '?'}   /   Class {data.get('class_name') or '?'}   /   PID {data.get('process_id')}   /   Backend {data['backend']}")
        self.path_badge.set(f"{'路径完整' if snapshot['path_complete'] else '路径不完整'} · {len(snapshot['locator_path'])} 层")
        self.hierarchy.delete(*self.hierarchy.get_children())
        parent=''
        for index,item in enumerate(snapshot['locator_path']):
            parent=self.hierarchy.insert(parent,'end',iid=str(index),text=item.get('name') or '<未命名>',values=(item.get('control_type') or '',item.get('automation_id') or '',item.get('class_name') or ''),open=True)
        if parent:
            self.hierarchy.selection_set(parent)
            self.hierarchy.see(parent)
        self.sync_actions()
        if not self.preview:
            try: self.highlight()
            except Exception: self.log('高亮失败：\n'+traceback.format_exc())

    def select_parent(self,event):
        item=self.hierarchy.identify_row(event.y)
        if item: self.request('select',{'index':int(item)})

    def backend_changed(self,event=None):
        self.stop_timer()
        self.snapshot=None
        self.clear_overlays()
        self.hierarchy.delete(*self.hierarchy.get_children())
        for name in ('Summary','All Properties','Generated Code','Control Tree'): self.text(name,'后端已切换，请重新捕获控件。')
        self.target_title.set('尚未捕获控件')
        self.target_detail.set('后端已切换，请重新捕获')
        self.path_badge.set('等待捕获')
        self.status.set('后端已切换，请重新捕获控件。')
        self.sync_actions()

    def capture(self,point=None):
        self.stop_timer()
        self.clear_overlays()
        if self.busy or self.preview:
            self.status.set('当前不能捕获：引擎忙碌或处于界面预览模式。')
            return
        self.request('capture',{'backend':self.backend.get(),'point':point if point is not None else cursor_pos()})

    def delayed(self):
        if self.busy or not self.ready: return
        self.stop_timer()
        self.clear_overlays()
        def tick(left):
            self.timer=None
            if left==0:
                self.capture()
            else:
                self.status.set(f'{left} 秒后捕获 · 请将鼠标移到目标控件')
                self.timer=self.after(1000,tick,left-1)
            self.sync_actions()
        tick(3)

    def stop_timer(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer=None

    def cancel(self):
        self.stop_timer()
        if self.busy:
            self.worker.cancel.set()
            self.status.set('已请求取消，等待当前底层调用返回。')
        else:
            self.status.set('倒数已取消；当前没有正在执行的任务。')
        self.sync_actions()

    def read_tree(self):
        depth,nodes,seconds=int(self.depth.get()),int(self.nodes.get()),float(self.seconds.get())
        if not (0<=depth<=20 and 1<=nodes<=10000 and 1<=seconds<=120):
            raise ValueError('深度 0–20；节点 1–10000；时间预算 1–120 秒。')
        self.request('tree',dict(depth=depth,nodes=nodes,seconds=seconds,scope=self.scope.get()))

    def copy_code(self):
        if self.snapshot is None: return
        self.clipboard_clear()
        self.clipboard_append(self.tabs['Generated Code'].get('1.0','end-1c'))
        self.status.set('完整父级定位代码已复制。')
        self.switch_tab('Generated Code')

    def clear_overlays(self):
        for overlay in self.overlays[:]:
            try: overlay.destroy()
            except tk.TclError: pass
        self.overlays.clear()

    def highlight(self):
        self.clear_overlays()
        if not self.snapshot or self.preview: return
        r=self.snapshot['element'].get('rectangle',{})
        if not r or r['right']<=r['left'] or r['bottom']<=r['top']: return
        overlay=tk.Toplevel(self)
        self.overlays.append(overlay)
        overlay.overrideredirect(True)
        overlay.attributes('-topmost',True)
        overlay.attributes('-transparentcolor','white')
        width,height=r['right']-r['left'],r['bottom']-r['top']
        overlay.geometry(f'{width}x{height}+0+0')
        canvas=tk.Canvas(overlay,bg='white',highlightthickness=0)
        canvas.pack(fill='both',expand=True)
        canvas.create_rectangle(2,2,max(3,width-3),max(3,height-3),outline='#3868ED',width=3)
        hwnd=user32.GetAncestor(overlay.winfo_id(),2) or overlay.winfo_id()
        set_style(hwnd,-20,get_style(hwnd,-20)|0x20|0x08000000|0x80|0x80000)
        overlay.update_idletasks()
        if not user32.SetWindowPos(hwnd,wintypes.HWND(-1),r['left'],r['top'],width,height,0x0010|0x0040):
            raise ctypes.WinError(ctypes.get_last_error())
        def remove():
            if overlay in self.overlays:
                self.overlays.remove(overlay)
                overlay.destroy()
        overlay.after(800,remove)

    def save_dialog(self,extension):
        try:
            OUT_DIR.mkdir(parents=True,exist_ok=True)
            folder=OUT_DIR
        except OSError as exc:
            self.log(str(exc))
            folder=Path.home()
        return filedialog.asksaveasfilename(parent=self,initialdir=str(folder),initialfile=f'element_{datetime.now():%Y%m%d_%H%M%S_%f}{extension}',defaultextension=extension,filetypes=[(extension[1:].upper(),'*'+extension)])

    def save_json(self):
        if self.snapshot is None: return
        filename=self.save_dialog('.json')
        if filename:
            Path(filename).write_text(json.dumps(self.snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
            self.status.set(f'已保存 JSON：{filename}')

    def screenshot(self):
        if self.snapshot is None: return
        from PIL import ImageGrab
        r=self.snapshot['element'].get('rectangle',{})
        if not r or r['right']<=r['left'] or r['bottom']<=r['top']:
            raise ValueError('控件矩形无效，请重新捕获。')
        self.clear_overlays()
        self.update_idletasks()
        image=ImageGrab.grab(bbox=(r['left'],r['top'],r['right'],r['bottom']),all_screens=True)
        try:
            filename=self.save_dialog('.png')
            if filename:
                image.save(filename)
                self.status.set(f'已保存截图：{filename}')
        finally:
            image.close()

    def close(self):
        self.closing=True
        self.stop_timer()
        if self.poll_timer is not None: self.after_cancel(self.poll_timer)
        self.clear_overlays()
        self.worker.stop()
        self.hotkeys.stopping.set()
        if self.hotkeys.is_alive(): self.hotkeys.join(timeout=0.2)
        self.destroy()


def enable_dpi():
    try:
        fn=bind(user32,'SetProcessDpiAwarenessContext',[ctypes.c_void_p],wintypes.BOOL)
        if fn(ctypes.c_void_p(-4)): return
    except (AttributeError,OSError): pass
    try:
        shcore=ctypes.WinDLL('shcore')
        bind(shcore,'SetProcessDpiAwareness',[ctypes.c_int],ctypes.c_long)(2)
    except (AttributeError,OSError):
        bind(user32,'SetProcessDPIAware',[],wintypes.BOOL)()


def main():
    preview='--preview' in sys.argv
    if not IS_WINDOWS and not preview:
        print('此工具的自动化功能仅支持 Windows。')
        return 1
    if IS_WINDOWS: enable_dpi()
    App(preview=preview).mainloop()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
