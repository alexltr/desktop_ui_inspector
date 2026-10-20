# -*- coding: utf-8 -*-
"""Desktop UI Inspector 2.1.0. Windows / Python 3.9+.
Install: python -m pip install pywinauto Pillow pywin32
All automation objects stay in one persistent MTA worker.
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
from tkinter import ttk, filedialog

VERSION = '2.1.0'
APP = 'Desktop UI Inspector'
OUT_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'DesktopUIInspector' / 'captures'
IS_WINDOWS = sys.platform == 'win32'


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
    longtype = ctypes.c_ssize_t
    suffix = 'PtrW' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'W'
    get_style = bind(user32, 'GetWindowLong'+suffix, [wintypes.HWND, ctypes.c_int], longtype)
    set_style = bind(user32, 'SetWindowLong'+suffix, [wintypes.HWND, ctypes.c_int, longtype], longtype)
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
    # Do not use HWND, PID, runtime ID, coordinates or ordinal indices for children.
    keys = ('automation_id', 'control_type', 'class_name', 'name') if data['backend'] == 'uia' else ('class_name', 'name')
    return {key: data[key] for key in keys if data.get(key) not in (None, '')}


def generated_code(snapshot):
    """Produce a complete, direct-parent-only path, failing closed on ambiguity."""
    path = snapshot.get('locator_path', snapshot.get('ancestors', []))
    top = snapshot.get('top_window', {})
    if not snapshot.get('path_complete'):
        return "# Full parent path could not be captured. Capture again.\nraise RuntimeError('Incomplete parent path; refusing to generate a partial locator.')\n"
    if not path or not top.get('handle'):
        return "raise RuntimeError('Top-level window handle unavailable; capture again.')\n"
    lines = [
        'from pywinauto import Desktop', 'import time', '',
        '# HWND/PID identify this running session only. Re-capture after restart.',
        '# Each step searches DIRECT children, never all descendants.',
        '# Zero matches are retried; multiple matches always raise an error.',
        'def direct_child(parent, criteria, step, timeout=10):',
        '    deadline = time.monotonic() + timeout',
        '    while True:',
        '        matches = []',
        '        for child in parent.children():',
        '            element = child.element_info',
        '            if all(getattr(element, key) == value for key, value in criteria.items()):',
        '                matches.append(child)',
        '        if len(matches) > 1:',
        "            raise RuntimeError(f'Step {step}: {len(matches)} direct children match {criteria!r}; add a distinguishing property or fix the application AutomationIds.')",
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
        label = f"Step {index}: {item.get('control_type') or item.get('class_name') or '?'} | {item.get('name') or '<unnamed>'} | {item.get('automation_id') or ''}"
        # repr() keeps names containing newlines/quotes from changing the source structure.
        lines += ['', f'# {label.replace(chr(10), " ").replace(chr(13), " ")}',
                  f'node_{index} = direct_child(node_{index-1}, {criteria!r}, step={index})']
    last = len(path)-1
    lines += ['', f'target = node_{last}',
              'if not target.is_visible() or not target.is_enabled():',
              "    raise RuntimeError('Target is not visible/enabled.')",
              'print(target.element_info)', '',
              '# Inspection only. Review the target before enabling any action.',
              '# target.set_edit_text("...")', '# target.click_input()']
    return '\n'.join(lines)+'\n'


class Cancelled(Exception):
    pass


class Worker(threading.Thread):
    def __init__(self, events):
        super().__init__(name='Inspector-MTA', daemon=True)
        self.events, self.commands = events, queue.Queue()
        self.cancel = threading.Event()
        self.stopping = threading.Event()
        self.current, self.chain, self.backend = None, [], None

    def check(self, deadline=None):
        if self.cancel.is_set() or self.stopping.is_set():
            raise Cancelled('Cancelled (after the current provider call returned).')
        if deadline is not None and time.monotonic() >= deadline:
            raise Cancelled('Time budget reached (cooperative, not a hard timeout).')

    def run(self):
        initialized = False
        try:
            hr = ole32.CoInitializeEx(None, 0)
            if hr not in (0, 1):
                raise RuntimeError(f'Cannot initialize MTA: 0x{hr & 0xffffffff:08X}')
            initialized = True
            # Import inside the same persistent apartment that owns every wrapper.
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
                raise RuntimeError('Inspector itself was captured. Point at the target application.')
            return self.capture(wrapper, backend, payload['point'])
        if self.current is None:
            raise RuntimeError('Capture an element first.')
        if operation == 'parent':
            if len(self.chain) <= 1:
                raise RuntimeError('Already at the application top-level window.')
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
        """Walk actual parent() links. Never infer a path from a descendant search."""
        top = wrapper.top_level_parent()
        if top.element_info.handle == user32.GetDesktopWindow():
            raise RuntimeError('Desktop-wide capture is disabled.')
        chain, path, warnings = [], [], []
        current, complete = wrapper, False
        # Equality uses backend element identity; no name/repr-based deduplication.
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
            warnings.append('Full path did not reach the application top-level window; code generation disabled.')
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
                    self.events.put(('notice', None, f'Hotkey {ident} failed: {ctypes.WinError(ctypes.get_last_error())}'))
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'{APP} {VERSION}')
        self.geometry('1280x820')
        self.minsize(1050, 650)
        self.events = queue.Queue()
        self.snapshot = None
        self.busy = self.ready = self.closing = self.paused = False
        self.ident, self.active = 0, None
        self.timer = self.poll_timer = None
        self.overlays = []
        self.build()
        self.worker = Worker(self.events)
        self.worker.start()
        self.hotkeys = Hotkeys(self.events)
        self.hotkeys.start()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_timer = self.after(60, self.poll)

    def build(self):
        self.backend = tk.StringVar(value='uia')
        self.status = tk.StringVar(value='Starting worker...')
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill='x')
        ttk.Label(bar, text='Backend:').pack(side='left')
        self.backend_box = ttk.Combobox(bar, textvariable=self.backend, values=('uia','win32'), state='readonly', width=8)
        self.backend_box.pack(side='left', padx=4)
        self.backend_box.bind('<<ComboboxSelected>>', self.backend_changed)
        for label, command in [('Capture in 3s',self.delayed), ('Parent (F9)',lambda:self.request('parent')), ('Highlight',self.highlight), ('Screenshot',self.screenshot), ('Save JSON',self.save_json), ('Load Properties',lambda:self.request('properties')), ('Copy Code',self.copy_code), ('Cancel',self.cancel)]:
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=2)
        bar2 = ttk.Frame(self, padding=(8,0,8,8))
        bar2.pack(fill='x')
        self.scope = tk.StringVar(value='captured')
        ttk.Label(bar2, text='Tree scope:').pack(side='left')
        ttk.Combobox(bar2, textvariable=self.scope, values=('captured','top window'), state='readonly', width=12).pack(side='left')
        self.depth, self.nodes, self.seconds = tk.StringVar(value='4'), tk.StringVar(value='300'), tk.StringVar(value='15')
        for label, variable in [('Depth',self.depth),('Nodes',self.nodes),('Budget (s)',self.seconds)]:
            ttk.Label(bar2, text=label).pack(side='left', padx=(8,3))
            ttk.Entry(bar2, textvariable=variable, width=6).pack(side='left')
        ttk.Button(bar2, text='Read Tree', command=self.read_tree).pack(side='left', padx=8)
        ttk.Label(bar2, text='Cancellation is cooperative; provider calls cannot be forcibly interrupted.').pack(side='left')
        pane = ttk.Panedwindow(self, orient='horizontal')
        pane.pack(fill='both', expand=True, padx=8)
        left, right = ttk.Frame(pane), ttk.Frame(pane)
        pane.add(left, weight=2)
        pane.add(right, weight=3)
        ttk.Label(left, text='Complete parent path (double-click to select)').pack(anchor='w')
        frame = ttk.Frame(left)
        frame.pack(fill='both', expand=True)
        self.hierarchy = ttk.Treeview(frame, columns=('type','id','class'), show='tree headings')
        for col, label, width in [('#0','Name',210),('type','Type',100),('id','Automation ID',160),('class','Class',130)]:
            self.hierarchy.heading(col,text=label)
            self.hierarchy.column(col,width=width,stretch=False)
        sy = ttk.Scrollbar(frame,command=self.hierarchy.yview)
        sx = ttk.Scrollbar(frame,orient='horizontal',command=self.hierarchy.xview)
        self.hierarchy.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        self.hierarchy.grid(row=0,column=0,sticky='nsew')
        sy.grid(row=0,column=1,sticky='ns')
        sx.grid(row=1,column=0,sticky='ew')
        frame.rowconfigure(0,weight=1)
        frame.columnconfigure(0,weight=1)
        self.hierarchy.bind('<Double-1>',self.select_parent)
        self.book = ttk.Notebook(right)
        self.book.pack(fill='both',expand=True)
        self.tabs = {}
        for name in ('Summary','All Properties','Generated Code','Control Tree','Log'):
            f = ttk.Frame(self.book)
            text = tk.Text(f,wrap='none',font=('Consolas',10))
            sy = ttk.Scrollbar(f,command=text.yview)
            sx = ttk.Scrollbar(f,orient='horizontal',command=text.xview)
            text.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
            text.grid(row=0,column=0,sticky='nsew')
            sy.grid(row=0,column=1,sticky='ns')
            sx.grid(row=1,column=0,sticky='ew')
            f.rowconfigure(0,weight=1)
            f.columnconfigure(0,weight=1)
            self.book.add(f,text=name)
            self.tabs[name] = text
        ttk.Label(self,textvariable=self.status,padding=8).pack(fill='x')
        ttk.Label(self,text='Ctrl+Shift+F8: capture | Ctrl+Shift+F9: current parent | Ctrl+Shift+F10: pause hotkeys').pack(anchor='e',padx=8)

    def text(self, name, value):
        widget = self.tabs[name]
        widget.delete('1.0','end')
        widget.insert('1.0',value)

    def log(self, value):
        widget = self.tabs['Log']
        widget.insert('end',f'[{datetime.now():%H:%M:%S}] {value}\n')
        if int(widget.index('end-1c').split('.')[0]) > 3000:
            widget.delete('1.0','501.0')
        widget.see('end')

    def report_callback_exception(self, kind, value, tb):
        self.status.set(str(value))
        self.log(''.join(traceback.format_exception(kind,value,tb)))

    def poll(self):
        try:
            for _ in range(100):
                try:
                    kind, ident, value = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.event(kind,ident,value)
                except Exception:
                    self.status.set('Interface update failed. See Log.')
                    self.log(traceback.format_exc())
        finally:
            if not self.closing:
                self.poll_timer = self.after(60,self.poll)

    def event(self, kind, ident, value):
        if kind == 'ready':
            self.ready = True
            self.status.set('Ready. Point at a target and press Ctrl+Shift+F8.')
        elif kind in ('fatal','notice'):
            if kind == 'fatal':
                self.ready = False
            self.status.set(value.splitlines()[-1])
            self.log(value)
        elif kind == 'hotkey':
            action, point = value
            if action == 'pause':
                self.paused = not self.paused
                self.status.set('Hotkeys paused.' if self.paused else 'Hotkeys resumed.')
            elif not self.paused:
                if action == 'capture':
                    self.capture(point)
                else:
                    self.request('parent')
        elif ident == self.active:
            if kind == 'result':
                operation, result, elapsed = value
                if operation in ('capture','parent','select'):
                    self.show(result)
                elif operation == 'properties':
                    self.snapshot['properties'] = result
                    self.text('All Properties',json.dumps(result,ensure_ascii=False,indent=2))
                elif operation == 'tree':
                    self.text('Control Tree',result)
                self.status.set(f'{operation}: completed in {elapsed:.2f}s')
                self.log(self.status.get())
            elif kind == 'error':
                self.status.set(value[0])
                self.log(value[1])
            elif kind == 'done':
                self.busy, self.active = False, None
                self.backend_box.configure(state='readonly')

    def request(self, operation, payload=None):
        if not self.ready or self.busy:
            self.status.set('Worker is busy or not ready.')
            return
        if operation != 'capture' and self.snapshot is None:
            self.status.set('Capture an element first.')
            return
        self.stop_timer()
        self.ident += 1
        self.active, self.busy = self.ident, True
        self.worker.cancel.clear()
        self.backend_box.configure(state='disabled')
        self.status.set(f'{operation}: working...')
        self.worker.commands.put((self.active,operation,payload or {}))

    def show(self, snapshot):
        self.snapshot = snapshot
        self.text('Summary',json.dumps(snapshot,ensure_ascii=False,indent=2))
        self.text('All Properties','Click Load Properties to read detailed properties.')
        self.text('Generated Code',generated_code(snapshot))
        self.text('Control Tree','Click Read Tree to inspect the current selection.')
        self.hierarchy.delete(*self.hierarchy.get_children())
        parent = ''
        for index, data in enumerate(snapshot['locator_path']):
            parent = self.hierarchy.insert(parent,'end',iid=str(index),text=data.get('name') or '<unnamed>',values=(data.get('control_type') or '',data.get('automation_id') or '',data.get('class_name') or ''),open=True)
        if parent:
            self.hierarchy.selection_set(parent)
            self.hierarchy.see(parent)
        try:
            self.highlight()
        except Exception:
            self.log('Highlight failed:\n'+traceback.format_exc())

    def select_parent(self, event):
        item = self.hierarchy.identify_row(event.y)
        if item:
            self.request('select',{'index':int(item)})

    def backend_changed(self, event=None):
        self.stop_timer()
        self.snapshot = None
        self.clear_overlays()
        self.hierarchy.delete(*self.hierarchy.get_children())
        for name in ('Summary','All Properties','Generated Code','Control Tree'):
            self.text(name,'')
        self.status.set('Backend changed. Capture a new element.')

    def capture(self, point=None):
        self.stop_timer()
        self.clear_overlays()
        self.request('capture',{'backend':self.backend.get(),'point':point if point is not None else cursor_pos()})

    def delayed(self):
        if self.busy or not self.ready:
            self.status.set('Worker is busy or not ready.')
            return
        self.stop_timer()
        self.clear_overlays()
        def tick(left):
            self.timer = None
            if left == 0:
                self.capture()
            else:
                self.status.set(f'Capture in {left}s. Move pointer onto the target.')
                self.timer = self.after(1000,tick,left-1)
        tick(3)

    def stop_timer(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None

    def cancel(self):
        self.stop_timer()
        if self.busy:
            self.worker.cancel.set()
            self.status.set('Cancellation requested; waiting for current provider call.')
        else:
            self.status.set('No operation active.')

    def read_tree(self):
        depth, nodes, seconds = int(self.depth.get()), int(self.nodes.get()), float(self.seconds.get())
        if not (0 <= depth <= 20 and 1 <= nodes <= 10000 and 1 <= seconds <= 120):
            raise ValueError('Depth: 0-20; nodes: 1-10000; seconds: 1-120.')
        self.request('tree',dict(depth=depth,nodes=nodes,seconds=seconds,scope=self.scope.get()))

    def copy_code(self):
        self.clipboard_clear()
        self.clipboard_append(self.tabs['Generated Code'].get('1.0','end-1c'))
        self.status.set('Generated code copied.')

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
        r = self.snapshot['element'].get('rectangle',{})
        if not r or r['right'] <= r['left'] or r['bottom'] <= r['top']:
            return
        overlay = tk.Toplevel(self)
        self.overlays.append(overlay)
        overlay.overrideredirect(True)
        overlay.attributes('-topmost',True)
        overlay.attributes('-transparentcolor','white')
        width, height = r['right']-r['left'], r['bottom']-r['top']
        overlay.geometry(f'{width}x{height}+0+0')
        canvas = tk.Canvas(overlay,bg='white',highlightthickness=0)
        canvas.pack(fill='both',expand=True)
        canvas.create_rectangle(2,2,max(3,width-3),max(3,height-3),outline='red',width=3)
        # Apply no-activate before mapping; native positioning supports negative monitors.
        hwnd = user32.GetAncestor(overlay.winfo_id(),2) or overlay.winfo_id()
        set_style(hwnd,-20,get_style(hwnd,-20)|0x20|0x08000000|0x80|0x80000)
        overlay.update_idletasks()
        if not user32.SetWindowPos(hwnd,wintypes.HWND(-1),r['left'],r['top'],width,height,0x0010|0x0040):
            raise ctypes.WinError(ctypes.get_last_error())
        def remove():
            if overlay in self.overlays:
                self.overlays.remove(overlay)
                overlay.destroy()
        overlay.after(800,remove)

    def save_dialog(self, extension):
        try:
            OUT_DIR.mkdir(parents=True,exist_ok=True)
            folder = OUT_DIR
        except OSError as exc:
            self.log(str(exc))
            folder = Path.home()
        return filedialog.asksaveasfilename(parent=self,initialdir=str(folder),initialfile=f'element_{datetime.now():%Y%m%d_%H%M%S_%f}{extension}',defaultextension=extension,filetypes=[(extension[1:].upper(),'*'+extension)])

    def save_json(self):
        if self.snapshot is None:
            raise ValueError('Capture an element first.')
        filename = self.save_dialog('.json')
        if filename:
            Path(filename).write_text(json.dumps(self.snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
            self.status.set(f'Saved: {filename}')

    def screenshot(self):
        if self.snapshot is None:
            raise ValueError('Capture an element first.')
        from PIL import ImageGrab
        r = self.snapshot['element'].get('rectangle',{})
        if not r or r['right'] <= r['left'] or r['bottom'] <= r['top']:
            raise ValueError('Invalid rectangle; capture again.')
        self.clear_overlays()
        self.update_idletasks()
        image = ImageGrab.grab(bbox=(r['left'],r['top'],r['right'],r['bottom']),all_screens=True)
        try:
            filename = self.save_dialog('.png')
            if filename:
                image.save(filename)
                self.status.set(f'Saved: {filename}')
        finally:
            image.close()

    def close(self):
        self.closing = True
        self.stop_timer()
        if self.poll_timer is not None:
            self.after_cancel(self.poll_timer)
        self.clear_overlays()
        self.worker.stop()
        self.hotkeys.stopping.set()
        self.hotkeys.join(timeout=0.2)
        self.destroy()


def main():
    if not IS_WINDOWS:
        print('This tool supports Windows only.')
        return 1
    try:
        dpi = bind(user32,'SetProcessDpiAwarenessContext',[ctypes.c_void_p],wintypes.BOOL)
        if not dpi(ctypes.c_void_p(-4)):
            raise OSError('DPI context not set')
    except (AttributeError,OSError):
        try:
            shcore = ctypes.WinDLL('shcore')
            bind(shcore,'SetProcessDpiAwareness',[ctypes.c_int],ctypes.c_long)(2)
        except (AttributeError,OSError):
            bind(user32,'SetProcessDPIAware',[],wintypes.BOOL)()
    App().mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
