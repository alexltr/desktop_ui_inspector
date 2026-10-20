# -*- coding: utf-8 -*-
"""Desktop UI Inspector 2.0.0, Windows only, Python 3.9+.
Install: python -m pip install pywinauto Pillow pywin32
Run: python desktop_ui_inspector_v2.py

All pywinauto wrappers belong exclusively to one persistent MTA worker.
Cancellation/deadlines are cooperative: a blocked provider call cannot be
interrupted safely in a Python thread. No target control is clicked.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP = 'Desktop UI Inspector'
VERSION = '2.0.0'
IS_WINDOWS = sys.platform == 'win32'
# Set before the worker imports pywinauto/comtypes.
sys.coinit_flags = 0
OUT_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'DesktopUIInspector' / 'captures'

if IS_WINDOWS:
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    ole32 = ctypes.WinDLL('ole32', use_last_error=True)

    def bind(dll, name, args, result):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
        return fn

    bind(user32, 'GetCursorPos', [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL)
    bind(user32, 'GetDesktopWindow', [], wintypes.HWND)
    bind(user32, 'GetAncestor', [wintypes.HWND, wintypes.UINT], wintypes.HWND)
    bind(user32, 'RegisterHotKey', [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL)
    bind(user32, 'UnregisterHotKey', [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    bind(user32, 'PeekMessageW', [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL)
    bind(user32, 'SetWindowPos', [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL)
    ptr_type = ctypes.c_ssize_t
    get_long_name = 'GetWindowLongPtrW' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'GetWindowLongW'
    set_long_name = 'SetWindowLongPtrW' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'SetWindowLongW'
    get_window_long = bind(user32, get_long_name, [wintypes.HWND, ctypes.c_int], ptr_type)
    set_window_long = bind(user32, set_long_name, [wintypes.HWND, ctypes.c_int, ptr_type], ptr_type)
    bind(kernel32, 'OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
    bind(kernel32, 'QueryFullProcessImageNameW', [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
    bind(kernel32, 'CloseHandle', [wintypes.HANDLE], wintypes.BOOL)
    bind(ole32, 'CoInitializeEx', [ctypes.c_void_p, wintypes.DWORD], ctypes.c_long)
    bind(ole32, 'CoUninitialize', [], None)


def cursor_pos():
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(point.x), int(point.y)


def json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_value(x) for x in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    return str(value)


def read_value(fn, errors, key, default=None):
    try:
        return fn()
    except Exception as exc:
        errors[key] = f'{type(exc).__name__}: {exc}'
        return default


def rect_dict(rect):
    if rect is None:
        return {}
    return dict(left=int(rect.left), top=int(rect.top), right=int(rect.right),
                bottom=int(rect.bottom), width=int(rect.right-rect.left),
                height=int(rect.bottom-rect.top))


def valid_rect(rect):
    return (isinstance(rect, dict) and
            all(isinstance(rect.get(k), int) for k in ('left', 'top', 'right', 'bottom')) and
            rect['right'] > rect['left'] and rect['bottom'] > rect['top'])


def light_info(wrapper, backend):
    errors = {}
    info = wrapper.element_info
    data = {'backend': backend}
    for name in ('name', 'automation_id', 'control_type', 'class_name', 'process_id', 'handle'):
        data[name] = json_value(read_value(lambda n=name: getattr(info, n), errors, name))
    if errors:
        data['read_errors'] = errors
    return data


def full_info(wrapper, backend):
    data = light_info(wrapper, backend)
    errors = data.setdefault('read_errors', {})
    info = wrapper.element_info
    for name in ('framework_id', 'runtime_id'):
        data[name] = json_value(read_value(lambda n=name: getattr(info, n), errors, name))
    data['rectangle'] = rect_dict(read_value(lambda: info.rectangle, errors, 'rectangle'))
    for name, fn in (('visible', wrapper.is_visible), ('enabled', wrapper.is_enabled)):
        data[name] = read_value(fn, errors, name)
    return data


def executable_path(pid):
    if not pid:
        return '', 'Process ID unavailable'
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
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


def detailed_properties(wrapper):
    errors, result = {}, {}
    info = wrapper.element_info
    # Explicit allowlist: do not inspect arbitrary internal COM objects.
    names = ('name', 'automation_id', 'control_type', 'class_name', 'framework_id',
             'process_id', 'handle', 'runtime_id', 'rich_text', 'access_key',
             'accelerator', 'legacy_name', 'legacy_value')
    for name in names:
        result[name] = json_value(read_value(lambda n=name: getattr(info, n), errors, name))
    for name, fn in (('texts', wrapper.texts), ('window_text', wrapper.window_text),
                     ('friendly_class_name', wrapper.friendly_class_name)):
        result[name] = json_value(read_value(fn, errors, name))
    result['wrapper_class'] = type(wrapper).__name__
    result['read_errors'] = errors
    return result


def generated_code(snapshot):
    data, top = snapshot['element'], snapshot['top_window']
    backend = data['backend']
    handle = top.get('handle')
    if not handle:
        return '# No valid top-level HWND. Capture a control inside an application window.\n'
    lines = [
        'from pywinauto import Desktop', '',
        '# Session-only locator: HWND and PID change when the application restarts.',
        '# Re-capture after restart. Validate title/class before using this locator.',
        f'desktop = Desktop(backend={backend!r})',
        f'window = desktop.window(handle={handle!r})',
        "window.wait('exists visible', timeout=10)",
        'root = window.wrapper_object()',
        f'if root.element_info.process_id != {top.get("process_id")!r}:',
        "    raise RuntimeError('Window process changed; capture again.')",
    ]
    if top.get('class_name'):
        lines += [f'if root.element_info.class_name != {top["class_name"]!r}:',
                  "    raise RuntimeError('Window class changed; capture again.')"]
    if snapshot.get('is_top_window'):
        lines.append('element = window')
    else:
        criteria = {}
        if backend == 'uia' and data.get('automation_id'):
            criteria['auto_id'] = data['automation_id']
        if data.get('name'):
            criteria['title'] = data['name']
        if backend == 'uia' and data.get('control_type'):
            criteria['control_type'] = data['control_type']
        elif data.get('class_name'):
            criteria['class_name'] = data['class_name']
        if not criteria:
            lines += ['# No usable selector. Inspect a stable parent and capture again.',
                      "raise RuntimeError('No reliable child selector available.')"]
            return '\n'.join(lines) + '\n'
        args = ', '.join(f'{k}={v!r}' for k, v in criteria.items())
        lines += ['# Ambiguous selectors raise an error; never silently select found_index=0.',
                  f'element = window.child_window({args})']
    lines += ["element.wait('exists visible enabled', timeout=10)",
              'target = element.wrapper_object()',
              'print(target.element_info)', '',
              '# Inspection only. Review the target before enabling any action.',
              '# target.click_input()', '',
              '# For reusable automation, replace the session HWND with a verified',
              '# stable top-window selector and narrow ambiguous children by parent.']
    return '\n'.join(lines) + '\n'


class Cancelled(Exception):
    pass


class InspectorWorker(threading.Thread):
    def __init__(self, events):
        super().__init__(name='Inspector-MTA', daemon=True)
        self.events = events
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.current = None
        self.chain = []
        self.backend = None
        self.snapshot = None
        self.desktop_class = None

    def check(self, deadline=None):
        if self.stop_event.is_set() or self.cancel_event.is_set():
            raise Cancelled('Operation cancelled; partial tree retained when available.')
        if deadline is not None and time.monotonic() >= deadline:
            raise Cancelled('Time budget reached; partial tree retained.')

    def submit(self, request_id, operation, payload):
        self.commands.put((request_id, operation, payload))

    def run(self):
        initialized = False
        try:
            hr = ole32.CoInitializeEx(None, 0)
            if hr not in (0, 1):
                raise RuntimeError(f'CoInitializeEx(MTA) failed: 0x{hr & 0xffffffff:08X}')
            initialized = True
            from pywinauto import Desktop
            self.desktop_class = Desktop
            self.events.put(('ready', None, None))
            while not self.stop_event.is_set():
                command = self.commands.get()
                if command is None:
                    break
                request_id, operation, payload = command
                started = time.monotonic()
                try:
                    self.check()
                    result = self.execute(operation, payload)
                    # Selection commits atomically; do not discard a committed result.
                    self.events.put(('result', request_id, (operation, result, time.monotonic()-started)))
                except Exception as exc:
                    self.events.put(('error', request_id, (str(exc), traceback.format_exc())))
                finally:
                    self.events.put(('done', request_id, None))
        except Exception:
            self.events.put(('fatal', None, traceback.format_exc()))
        finally:
            # Release wrappers while their owning apartment is still alive.
            self.current = self.snapshot = None
            self.chain.clear()
            self.desktop_class = None
            if initialized:
                ole32.CoUninitialize()

    def execute(self, operation, payload):
        if operation == 'capture':
            backend = payload['backend']
            x, y = payload['point']
            wrapper = self.desktop_class(backend=backend).from_point(x, y)
            if wrapper.element_info.process_id == os.getpid():
                raise RuntimeError('Inspector itself was captured. Use the hotkey over the target or delayed capture.')
            if payload.get('parent'):
                parent = wrapper.parent()
                if parent is None:
                    raise RuntimeError('This element has no parent.')
                wrapper = parent
            return self.select(wrapper, backend, (x, y))
        if self.current is None:
            raise RuntimeError('Capture an element first.')
        if operation == 'parent':
            parent = self.current.parent()
            if parent is None or parent.element_info.handle == user32.GetDesktopWindow():
                raise RuntimeError('Already at the application top level; desktop selection is disabled.')
            return self.select(parent, self.backend)
        if operation == 'select':
            index = payload['index']
            if not 0 <= index < len(self.chain):
                raise RuntimeError('Hierarchy changed. Capture again.')
            return self.select(self.chain[index], self.backend)
        if operation == 'properties':
            return detailed_properties(self.current)
        if operation == 'tree':
            root = self.current if payload['scope'] == 'captured' else self.current.top_level_parent()
            if root.element_info.handle == user32.GetDesktopWindow():
                raise RuntimeError('Desktop-wide tree reading is disabled.')
            return self.read_tree(root, payload)
        raise ValueError(f'Unknown operation: {operation}')

    def select(self, wrapper, backend, point=None):
        self.check()
        if wrapper.element_info.handle == user32.GetDesktopWindow():
            raise RuntimeError('Desktop selection is disabled. Capture an application control.')
        data = full_info(wrapper, backend)
        data['captured_at'] = datetime.now().isoformat(timespec='seconds')
        if point is not None:
            data['cursor'] = {'x': point[0], 'y': point[1]}
        path, error = executable_path(data.get('process_id'))
        data['executable'] = path
        if error:
            data.setdefault('read_errors', {})['executable'] = error
        top = wrapper.top_level_parent()
        if top.element_info.handle == user32.GetDesktopWindow():
            raise RuntimeError('No application top-level window could be determined.')
        top_data = light_info(top, backend)
        chain, chain_data, seen = [], [], set()
        current = wrapper
        warnings = []
        for _ in range(30):
            self.check()
            if current is None or current.element_info.handle == user32.GetDesktopWindow():
                break
            # Use actual identity, not repr(), which may repeat for different controls.
            if backend == 'uia':
                runtime = read_value(lambda: current.element_info.runtime_id, {}, 'runtime_id')
                identity = ('uia', tuple(runtime)) if runtime else ('object', id(current.element_info))
            else:
                identity = ('win32', current.element_info.handle)
            if identity in seen:
                warnings.append('Repeated ancestor encountered; hierarchy stopped.')
                break
            seen.add(identity)
            chain.append(current)
            chain_data.append(light_info(current, backend))
            if current == top:
                break
            try:
                current = current.parent()
            except Exception as exc:
                warnings.append(f'Ancestor read failed: {exc}')
                break
        else:
            warnings.append('Ancestor limit (30) reached.')
        chain.reverse()
        chain_data.reverse()
        snapshot = {'element': data, 'top_window': top_data,
                    'is_top_window': wrapper == top,
                    'ancestors': chain_data, 'warnings': warnings}
        # Commit only after all required reads succeed and cancellation is checked.
        self.check()
        self.current, self.backend = wrapper, backend
        self.chain, self.snapshot = chain, snapshot
        return snapshot

    def read_tree(self, root, options):
        max_nodes, max_depth = options['max_nodes'], options['max_depth']
        deadline = time.monotonic() + options['seconds']
        lines, count, truncated, errors = [], 0, False, []
        reason = ''
        def walk(wrapper, depth):
            nonlocal count, truncated
            self.check(deadline)
            if count >= max_nodes:
                truncated = True
                return
            count += 1
            data = light_info(wrapper, self.backend)
            self.check(deadline)
            name = str(data.get('name') or '<unnamed>').replace('\r', ' ').replace('\n', ' ')
            lines.append('    '*depth + f"[{data.get('control_type') or '?'}] {name} "
                         f"(auto_id={data.get('automation_id')!r}, class={data.get('class_name')!r})")
            if data.get('read_errors'):
                errors.append({'node': count, 'errors': data['read_errors']})
            if depth >= max_depth:
                # Do not enumerate children merely to test whether the depth boundary has children.
                truncated = True
                return
            self.check(deadline)
            try:
                children = wrapper.children()
            except Exception as exc:
                errors.append({'node': count, 'children_error': str(exc)})
                return
            self.check(deadline)
            for child in children:
                if count >= max_nodes:
                    truncated = True
                    break
                walk(child, depth+1)
        try:
            walk(root, 0)
        except Cancelled as exc:
            truncated, reason = True, str(exc)
        header = f'Nodes: {count}; scope: {options["scope"]}; max depth: {max_depth}; max nodes: {max_nodes}'
        if truncated:
            header += '\nINCOMPLETE / boundary reached: ' + (reason or 'Node or depth boundary reached; deeper nodes not inspected.')
        if errors:
            truncated = True
            header += '\nINCOMPLETE: some properties or child lists could not be read.'
            header += '\nRead errors: ' + json.dumps(errors, ensure_ascii=False)
        return {'text': header + '\n\n' + '\n'.join(lines), 'nodes': count, 'incomplete': truncated}

    def stop(self):
        self.stop_event.set()
        self.cancel_event.set()
        self.commands.put(None)


class Hotkeys(threading.Thread):
    def __init__(self, events):
        super().__init__(name='GlobalHotkeys', daemon=True)
        self.events = events
        self.stop_event = threading.Event()

    def run(self):
        registered = []
        mapping = {1001: 'capture', 1002: 'parent', 1003: 'pause'}
        try:
            for ident, vk in ((1001, 0x77), (1002, 0x78), (1003, 0x79)):
                # MOD_NOREPEAT prevents repeated work while a key is held down.
                if user32.RegisterHotKey(None, ident, 0x0002 | 0x0004 | 0x4000, vk):
                    registered.append(ident)
                else:
                    self.events.put(('notice', None, f'Hotkey {ident} registration failed: {ctypes.WinError(ctypes.get_last_error())}'))
            msg = wintypes.MSG()
            while not self.stop_event.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0312 and msg.wParam in mapping:
                        action = mapping[msg.wParam]
                        point = cursor_pos() if action == 'capture' else None
                        self.events.put(('hotkey', None, (action, point)))
                self.stop_event.wait(0.03)
        except Exception:
            self.events.put(('notice', None, traceback.format_exc()))
        finally:
            for ident in registered:
                user32.UnregisterHotKey(None, ident)

    def stop(self):
        self.stop_event.set()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'{APP} {VERSION}')
        self.geometry('1240x820')
        self.minsize(1000, 650)
        self.events = queue.Queue()
        self.snapshot = None
        self.ready = self.busy = self.closing = self.paused = False
        self.request_id = 0
        self.active_id = None
        self.countdown_id = self.poll_id = None
        self.overlays = []
        self.hierarchy_indexes = {}
        self.build_ui()
        self.worker = InspectorWorker(self.events)
        self.worker.start()
        self.hotkeys = Hotkeys(self.events)
        self.hotkeys.start()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = self.after(60, self.poll)

    def build_ui(self):
        self.backend = tk.StringVar(value='uia')
        self.status = tk.StringVar(value='Starting UIA worker...')
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill='x')
        ttk.Label(bar, text='Backend:').pack(side='left')
        self.backend_box = ttk.Combobox(bar, textvariable=self.backend, values=('uia', 'win32'), state='readonly', width=8)
        self.backend_box.pack(side='left', padx=5)
        self.backend_box.bind('<<ComboboxSelected>>', self.backend_changed)
        buttons = [('Capture in 3s', self.delayed_capture), ('Parent (F9)', self.parent),
                   ('Highlight', self.highlight), ('Screenshot', self.screenshot),
                   ('Save JSON', self.save_json), ('Load Properties', self.load_properties),
                   ('Cancel', self.cancel)]
        for text, command in buttons:
            ttk.Button(bar, text=text, command=command).pack(side='left', padx=2)
        treebar = ttk.Frame(self, padding=(8, 0, 8, 8))
        treebar.pack(fill='x')
        self.scope = tk.StringVar(value='captured')
        ttk.Label(treebar, text='Tree scope:').pack(side='left')
        ttk.Combobox(treebar, textvariable=self.scope, values=('captured', 'top window'), state='readonly', width=12).pack(side='left', padx=5)
        self.depth = tk.StringVar(value='4')
        self.nodes = tk.StringVar(value='300')
        self.seconds = tk.StringVar(value='15')
        for title, var, width in [('Depth', self.depth, 4), ('Nodes', self.nodes, 6), ('Budget (s)', self.seconds, 5)]:
            ttk.Label(treebar, text=title).pack(side='left', padx=(8, 3))
            ttk.Entry(treebar, textvariable=var, width=width).pack(side='left')
        ttk.Button(treebar, text='Read Tree', command=self.refresh_tree).pack(side='left', padx=8)
        ttk.Label(treebar, text='Cancellation / budget are cooperative, not a hard provider timeout.').pack(side='left')
        pane = ttk.Panedwindow(self, orient='horizontal')
        pane.pack(fill='both', expand=True, padx=8)
        left, right = ttk.Frame(pane), ttk.Frame(pane)
        pane.add(left, weight=2)
        pane.add(right, weight=3)
        ttk.Label(left, text='Element hierarchy (double-click to select)').pack(anchor='w')
        holder = ttk.Frame(left)
        holder.pack(fill='both', expand=True)
        self.hierarchy = ttk.Treeview(holder, columns=('type', 'id', 'class'), show='tree headings')
        for col, title, width in [('#0', 'Name', 200), ('type', 'Type', 100), ('id', 'Automation ID', 130), ('class', 'Class', 130)]:
            self.hierarchy.heading(col, text=title)
            self.hierarchy.column(col, width=width, stretch=False)
        sy = ttk.Scrollbar(holder, command=self.hierarchy.yview)
        sx = ttk.Scrollbar(holder, orient='horizontal', command=self.hierarchy.xview)
        self.hierarchy.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.hierarchy.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.hierarchy.bind('<Double-1>', self.select_ancestor)
        self.book = ttk.Notebook(right)
        self.book.pack(fill='both', expand=True)
        self.summary = self.text_tab('Summary')
        self.props = self.text_tab('All Properties')
        self.code = self.text_tab('Generated Code')
        self.tree = self.text_tab('Control Tree')
        self.log = self.text_tab('Log')
        foot = ttk.Frame(self, padding=8)
        foot.pack(fill='x')
        ttk.Label(foot, textvariable=self.status).pack(side='left')
        ttk.Label(foot, text='Ctrl+Shift+F8 capture | F9 parent | F10 pause').pack(side='right')

    def text_tab(self, title):
        frame = ttk.Frame(self.book)
        text = tk.Text(frame, wrap='none', font=('Consolas', 10), undo=False)
        sy = ttk.Scrollbar(frame, command=text.yview)
        sx = ttk.Scrollbar(frame, orient='horizontal', command=text.xview)
        text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        text.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.book.add(frame, text=title)
        return text

    def set_text(self, widget, value):
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)

    def write_log(self, value):
        self.log.insert('end', f'[{datetime.now():%H:%M:%S}] {value}\n')
        if int(self.log.index('end-1c').split('.')[0]) > 3000:
            self.log.delete('1.0', '501.0')
        self.log.see('end')

    def report_callback_exception(self, exc_type, exc_value, tb):
        detail = ''.join(traceback.format_exception(exc_type, exc_value, tb))
        if hasattr(self, 'log') and not self.closing:
            self.report('Interface action failed. See Log.', detail)
        else:
            sys.stderr.write(detail)

    def report(self, message, detail=None):
        self.status.set(message.splitlines()[0] if message else 'Error')
        self.write_log(detail or message)

    def poll(self):
        try:
            for _ in range(100):
                try:
                    kind, ident, value = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle_event(kind, ident, value)
                except Exception:
                    self.report('Interface update failed. See Log.', traceback.format_exc())
        finally:
            if not self.closing:
                self.poll_id = self.after(60, self.poll)

    def handle_event(self, kind, ident, value):
        if kind == 'ready':
            self.ready = True
            self.status.set('Ready. Point at a target and press Ctrl+Shift+F8.')
        elif kind == 'fatal':
            self.ready = False
            self.report('Worker failed. Check dependencies and Log.', value)
        elif kind == 'notice':
            self.report(value)
        elif kind == 'hotkey':
            action, point = value
            if action == 'pause':
                self.paused = not self.paused
                self.status.set('Hotkeys paused.' if self.paused else 'Hotkeys resumed.')
            elif not self.paused:
                if action == 'capture':
                    self.capture(point)
                else:
                    self.parent()
        elif ident == self.active_id:
            if kind == 'result':
                operation, result, elapsed = value
                if operation in ('capture', 'parent', 'select'):
                    self.show_snapshot(result)
                elif operation == 'properties':
                    self.snapshot['properties'] = result
                    self.set_text(self.props, json.dumps(result, ensure_ascii=False, indent=2))
                elif operation == 'tree':
                    self.set_text(self.tree, result['text'])
                self.status.set(f'{operation}: completed in {elapsed:.2f}s' + (' (partial tree)' if operation == 'tree' and result['incomplete'] else ''))
                self.write_log(self.status.get())
            elif kind == 'error':
                self.report(value[0], value[1])
            elif kind == 'done':
                self.busy = False
                self.active_id = None
                self.backend_box.configure(state='readonly')

    def submit(self, operation, payload=None):
        if not self.ready:
            self.report('Worker is not ready. Check Log.')
            return False
        if self.busy:
            self.status.set('An operation is active. Cancel it before starting another.')
            return False
        self.request_id += 1
        self.active_id = self.request_id
        self.busy = True
        self.backend_box.configure(state='disabled')
        self.worker.cancel_event.clear()
        self.status.set(f'{operation}: working...')
        self.worker.submit(self.active_id, operation, payload or {})
        return True

    def capture(self, point=None):
        if self.countdown_id is not None:
            self.after_cancel(self.countdown_id)
            self.countdown_id = None
        if self.busy:
            self.status.set('An operation is active; capture ignored.')
            return
        self.clear_overlays()
        try:
            self.submit('capture', {'backend': self.backend.get(), 'point': point if point is not None else cursor_pos()})
        except Exception as exc:
            self.report(f'Capture failed: {exc}')

    def delayed_capture(self):
        if self.busy or not self.ready:
            self.status.set('Worker is busy or not ready.')
            return
        if self.countdown_id is not None:
            self.after_cancel(self.countdown_id)
        self.clear_overlays()
        def tick(remaining):
            self.countdown_id = None
            if self.closing:
                return
            if remaining <= 0:
                self.capture()
                return
            self.status.set(f'Capture in {remaining}s. Move the pointer onto the target.')
            self.countdown_id = self.after(1000, tick, remaining-1)
        tick(3)

    def parent(self):
        if self.snapshot:
            self.submit('parent')
        else:
            self.status.set('Capture an element first.')

    def select_ancestor(self, event=None):
        item = self.hierarchy.identify_row(event.y) if event else self.hierarchy.focus()
        if item in self.hierarchy_indexes:
            self.submit('select', {'index': self.hierarchy_indexes[item]})

    def backend_changed(self, event=None):
        self.cancel_countdown()
        self.snapshot = None
        self.clear_overlays()
        self.hierarchy.delete(*self.hierarchy.get_children())
        self.hierarchy_indexes.clear()
        for widget in (self.summary, self.props, self.code, self.tree):
            self.set_text(widget, '')
        self.status.set('Backend changed. Capture a new element.')

    def show_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.set_text(self.summary, json.dumps(snapshot, ensure_ascii=False, indent=2))
        self.set_text(self.props, 'Detailed properties are loaded on demand. Click Load Properties.\nUnavailable values are null; read_errors contains diagnostics.')
        self.set_text(self.code, generated_code(snapshot))
        self.set_text(self.tree, 'Click Read Tree to inspect this selection.')
        self.hierarchy.delete(*self.hierarchy.get_children())
        self.hierarchy_indexes.clear()
        parent = ''
        for index, data in enumerate(snapshot['ancestors']):
            parent = self.hierarchy.insert(parent, 'end', text=data.get('name') or '<unnamed>',
                values=(data.get('control_type') or '', data.get('automation_id') or '', data.get('class_name') or ''), open=True)
            self.hierarchy_indexes[parent] = index
        if parent:
            self.hierarchy.selection_set(parent)
            self.hierarchy.see(parent)
        # Highlight is optional; its failure must not invalidate a successful capture.
        try:
            self.highlight()
        except Exception:
            self.write_log('Highlight failed:\n' + traceback.format_exc())

    def load_properties(self):
        if self.snapshot:
            self.submit('properties')
        else:
            self.status.set('Capture an element first.')

    def refresh_tree(self):
        if not self.snapshot:
            self.status.set('Capture an element first.')
            return
        try:
            depth, nodes, seconds = int(self.depth.get()), int(self.nodes.get()), float(self.seconds.get())
            if not (0 <= depth <= 20 and 1 <= nodes <= 10000 and 1 <= seconds <= 120):
                raise ValueError('Depth: 0-20; nodes: 1-10000; budget: 1-120 seconds.')
            self.submit('tree', {'scope': 'captured' if self.scope.get() == 'captured' else 'top window',
                                 'max_depth': depth, 'max_nodes': nodes, 'seconds': seconds})
        except ValueError as exc:
            self.report(f'Invalid tree settings: {exc}')

    def cancel_countdown(self):
        if self.countdown_id is not None:
            self.after_cancel(self.countdown_id)
            self.countdown_id = None

    def cancel(self):
        self.cancel_countdown()
        if self.busy:
            self.worker.cancel_event.set()
            self.status.set('Cancellation requested; waiting for the current provider call to return.')
        else:
            self.status.set('No operation active.')

    def clear_overlays(self):
        for overlay in self.overlays[:]:
            try:
                overlay.destroy()
            except tk.TclError:
                pass
        self.overlays.clear()

    def highlight(self):
        self.clear_overlays()
        if not self.snapshot:
            return
        rect = self.snapshot['element'].get('rectangle', {})
        if not valid_rect(rect):
            self.write_log('Highlight skipped: invalid or unavailable rectangle.')
            return
        overlay = tk.Toplevel(self)
        self.overlays.append(overlay)
        overlay.withdraw()
        overlay.overrideredirect(True)
        overlay.attributes('-topmost', True)
        overlay.attributes('-transparentcolor', 'white')
        width, height = rect['right']-rect['left'], rect['bottom']-rect['top']
        overlay.geometry(f'{width}x{height}+0+0')
        canvas = tk.Canvas(overlay, bg='white', highlightthickness=0)
        canvas.pack(fill='both', expand=True)
        canvas.create_rectangle(2, 2, max(3, width-3), max(3, height-3), outline='red', width=3)
        overlay.update_idletasks()
        hwnd = user32.GetAncestor(overlay.winfo_id(), 2) or overlay.winfo_id()
        # Click-through, no activation, tool window. Native positioning handles negative coordinates.
        style = get_window_long(hwnd, -20)
        set_window_long(hwnd, -20, style | 0x20 | 0x08000000 | 0x80 | 0x80000)
        if not user32.SetWindowPos(hwnd, wintypes.HWND(-1), rect['left'], rect['top'], width, height, 0x0010 | 0x0040):
            self.clear_overlays()
            raise ctypes.WinError(ctypes.get_last_error())
        # SetWindowPos(SHOWWINDOW) displays the native window without taking focus.
        def remove():
            if overlay in self.overlays:
                self.overlays.remove(overlay)
                try:
                    overlay.destroy()
                except tk.TclError:
                    pass
        overlay.after(800, remove)

    def save_dialog(self, extension):
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            initialdir = OUT_DIR
        except OSError as exc:
            self.write_log(f'Capture folder unavailable: {exc}')
            initialdir = Path.home()
        name = f'element_{datetime.now():%Y%m%d_%H%M%S_%f}{extension}'
        return filedialog.asksaveasfilename(parent=self, initialdir=str(initialdir), initialfile=name,
            defaultextension=extension, filetypes=[(extension[1:].upper(), '*'+extension)])

    def save_json(self):
        if not self.snapshot:
            self.status.set('Capture an element first.')
            return
        try:
            filename = self.save_dialog('.json')
            if filename:
                Path(filename).write_text(json.dumps(self.snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
                self.status.set(f'Saved: {filename}')
        except Exception:
            self.report('JSON save failed. See Log.', traceback.format_exc())

    def screenshot(self):
        if not self.snapshot:
            self.status.set('Capture an element first.')
            return
        try:
            from PIL import ImageGrab
            rect = self.snapshot['element'].get('rectangle', {})
            if not valid_rect(rect):
                raise ValueError('Invalid or unavailable element rectangle. Capture again.')
            self.clear_overlays()
            self.update_idletasks()
            # Capture before the dialog opens, so the dialog does not cover the target.
            image = ImageGrab.grab(bbox=(rect['left'], rect['top'], rect['right'], rect['bottom']), all_screens=True)
            try:
                filename = self.save_dialog('.png')
                if filename:
                    image.save(filename)
                    self.status.set(f'Saved: {filename}')
            finally:
                image.close()
        except Exception:
            self.report('Screenshot failed. See Log.', traceback.format_exc())

    def close(self):
        self.closing = True
        self.cancel_countdown()
        if self.poll_id is not None:
            self.after_cancel(self.poll_id)
        self.clear_overlays()
        self.hotkeys.stop()
        self.worker.stop()
        # Do not freeze the Tk thread joining a potentially blocked provider.
        self.hotkeys.join(timeout=0.2)
        self.destroy()


def enable_dpi_awareness():
    try:
        fn = bind(user32, 'SetProcessDpiAwarenessContext', [ctypes.c_void_p], wintypes.BOOL)
        if fn(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL('shcore', use_last_error=True)
        fn = bind(shcore, 'SetProcessDpiAwareness', [ctypes.c_int], ctypes.c_long)
        if fn(2) in (0, 1):
            return
    except (AttributeError, OSError):
        pass
    try:
        bind(user32, 'SetProcessDPIAware', [], wintypes.BOOL)()
    except (AttributeError, OSError):
        pass


def main():
    if not IS_WINDOWS:
        print('This tool supports Windows only.')
        return 1
    enable_dpi_awareness()
    App().mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
