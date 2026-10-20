# -*- coding: utf-8 -*-
"""Desktop UI Inspector 2.2.3. Windows / Python 3.9+.
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

VERSION = '2.2.3'
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


class InspectorBase(tk.Tk):
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



# Appearance is deliberately independent of the UIA worker.
THEMES = {
 'light': dict(bg='#EAF0F8', card='#FFFFFF', text='#10243A', muted='#405773',
               accent='#185ADB', hover='#D8E8FF', selected='#C6DCFF', line='#B8C8DD',
               editor='#FFFFFF', thumb='#8FA6C2', primaryhover='#0845BC'),
 'dark': dict(bg='#0C1422', card='#162338', text='#EDF4FF', muted='#B0C3DE',
              accent='#3979EF', hover='#2B4469', selected='#284F86', line='#3C5575',
              editor='#101C2E', thumb='#6D8CB5', primaryhover='#5A92FF'),
}
TABS = {'Summary':'概览','All Properties':'详细属性','Generated Code':'定位代码','Control Tree':'控件树','Log':'运行日志'}


def mix_color(start, end, fraction):
    fraction = max(0.0, min(1.0, fraction))
    values = [round(int(start[i:i+2],16)*(1-fraction)+int(end[i:i+2],16)*fraction) for i in (1,3,5)]
    return '#' + ''.join(f'{v:02x}' for v in values)


class HoverButton(tk.Button):
    """110 ms color transition; no layout movement or animation threads."""
    def __init__(self, parent, **kwargs):
        self.animation = None
        self.hovered = False
        self.base_color = self.over_color = '#FFFFFF'
        super().__init__(parent, bd=0, relief='flat', highlightthickness=0,
                         padx=10, pady=5, cursor='hand2', takefocus=True, **kwargs)
        self.bind('<Enter>', lambda e:self.hover(True))
        self.bind('<Leave>', lambda e:self.hover(False))
        self.bind('<FocusIn>', lambda e:self.hover(True))
        self.bind('<FocusOut>', lambda e:self.hover(False))
        self.bind('<Destroy>', self.cleanup)

    def cleanup(self, event=None):
        if self.animation is not None:
            try: self.after_cancel(self.animation)
            except tk.TclError: pass
            self.animation = None

    def palette(self, base, over, foreground, disabled):
        self.cleanup()
        self.base_color, self.over_color = base, over
        self.configure(bg=over if self.hovered else base, fg=foreground,
                       activebackground=over, activeforeground=foreground,
                       disabledforeground=disabled)

    def hover(self, entered):
        self.hovered = entered
        self.cleanup()
        if str(self.cget('state')) == 'disabled':
            return
        start = self.cget('background')
        target = self.over_color if entered else self.base_color
        def tick(step=1):
            self.animation = None
            if not self.winfo_exists(): return
            if str(self.cget('state')) == 'disabled':
                self.configure(bg=self.base_color)
                return
            self.configure(bg=mix_color(start,target,step/7))
            if step < 7:
                self.animation = self.after(16,tick,step+1)
        tick()


class SlimScrollbar(tk.Canvas):
    def __init__(self,parent,command,orient='vertical'):
        super().__init__(parent,width=9,height=9,bd=0,highlightthickness=0,takefocus=False)
        self.command=command
        self.horizontal=orient=='horizontal'
        self.first,self.last=0.,1.
        self.drag_origin=None
        self.colors=THEMES['light']
        self.bind('<Configure>',lambda e:self.draw())
        self.bind('<Button-1>',self.press)
        self.bind('<B1-Motion>',self.drag)
        self.bind('<ButtonRelease-1>',lambda e:setattr(self,'drag_origin',None))
        self.bind('<MouseWheel>',self.wheel)

    def theme(self,c):
        self.colors=c
        self.configure(bg=c['editor'])
        self.draw()

    def set(self,first,last):
        self.first=max(0.,min(1.,float(first)))
        self.last=max(self.first,min(1.,float(last)))
        if self.first==0 and self.last==1: self.grid_remove()
        else: self.grid()
        self.draw()

    def metrics(self):
        length=max(1,self.winfo_width() if self.horizontal else self.winfo_height())
        thumb=min(length,max(24,(self.last-self.first)*length))
        travel=length-thumb
        span=max(0.,1-(self.last-self.first))
        start=self.first/span*travel if span else 0
        return thumb,travel,span,start

    def draw(self):
        self.delete('all')
        thumb,travel,span,start=self.metrics()
        if thumb<6: return
        if self.horizontal:
            y=self.winfo_height()/2
            self.create_line(start+3,y,start+thumb-3,y,fill=self.colors['thumb'],width=5,capstyle='round')
        else:
            x=self.winfo_width()/2
            self.create_line(x,start+3,x,start+thumb-3,fill=self.colors['thumb'],width=5,capstyle='round')

    def press(self,e):
        pos=e.x if self.horizontal else e.y
        thumb,travel,span,start=self.metrics()
        value=self.first
        if not start<=pos<=start+thumb:
            value=max(0.,min(span,(pos-thumb/2)/travel*span)) if travel else 0.
            self.command('moveto',value)
        self.drag_origin=(pos,value)

    def drag(self,e):
        if self.drag_origin is None: return
        pos=e.x if self.horizontal else e.y
        thumb,travel,span,start=self.metrics()
        value=self.drag_origin[1]+(pos-self.drag_origin[0])/travel*span if travel else 0.
        self.command('moveto',max(0.,min(span,value)))

    def wheel(self,e):
        if e.delta:
            self.command('scroll',-3 if e.delta>0 else 3,'units')
        return 'break'


class App(InspectorBase):
    def build(self):
        self.theme_name='light'
        self.surfaces=[]
        self.labels=[]
        self.action_buttons=[]
        self.tab_buttons={}
        self.tab_frames={}
        self.scrollbars=[]
        self.current_tab='Summary'
        self.backend=tk.StringVar(value='uia')
        self.scope=tk.StringVar(value='captured')
        self.depth=tk.StringVar(value='4')
        self.nodes=tk.StringVar(value='300')
        self.seconds=tk.StringVar(value='15')
        self.status=tk.StringVar(value='正在初始化引擎…')
        self.target_name=tk.StringVar(value='尚未捕获控件')
        self.target_meta=tk.StringVar(value='Ctrl + Shift + F8 捕获鼠标所在控件')
        self.path_state=tk.StringVar(value='等待捕获')
        self.style=ttk.Style(self)
        self.style.theme_use('clam')
        self.option_add('*Font',('Microsoft YaHei UI',10))
        self.geometry(f'{min(1380,max(1000,self.winfo_screenwidth()-60))}x{min(860,max(650,self.winfo_screenheight()-100))}')
        self.minsize(1050,650)
        root=self.surface(self,'bg')
        root.pack(fill='both',expand=True,padx=8,pady=6)
        root.columnconfigure(0,weight=1)
        root.rowconfigure(3,weight=1)
        header=self.surface(root,'bg')
        header.grid(row=0,column=0,sticky='ew',pady=(0,5))
        header.columnconfigure(1,weight=1)
        self.label(header,APP,font=('Segoe UI',14,'bold'),bg='bg').grid(row=0,column=0,sticky='w')
        self.label(header,'V'+VERSION,muted=True,bg='bg').grid(row=0,column=1,sticky='w',padx=10)
        self.label(header,'引擎',muted=True,bg='bg').grid(row=0,column=2,padx=5)
        self.backend_box=ttk.Combobox(header,textvariable=self.backend,values=('uia','win32'),width=7,state='readonly')
        self.backend_box.grid(row=0,column=3,padx=5)
        self.backend_box.bind('<<ComboboxSelected>>',self.backend_changed)
        self.theme_button=self.button(header,'切换深色',self.toggle_theme)
        self.theme_button.grid(row=0,column=4,padx=(8,0))
        toolbar=self.surface(root)
        toolbar.grid(row=1,column=0,sticky='ew',pady=(0,5))
        for col,(title,fn,kind,action) in enumerate([
            ('捕获 · 3 秒',self.delayed,'primary','capture'),
            ('上一级',lambda:self.request('parent'),'normal','target'),
            ('高亮',self.highlight,'normal','target'),
            ('详细属性',lambda:self.request('properties'),'normal','target'),
            ('保存 JSON',self.save_json,'normal','target'),
            ('截图',self.screenshot,'normal','target'),
            ('复制代码',self.copy_code,'normal','target'),
            ('取消',self.cancel,'normal','cancel')]):
            self.button(toolbar,title,fn,kind,action).grid(row=0,column=col,padx=(0 if col==0 else 3,0),pady=3)
        summary=self.surface(root)
        summary.grid(row=2,column=0,sticky='ew',pady=(0,5))
        summary.columnconfigure(0,weight=1)
        self.label(summary,variable=self.target_name,font=('Microsoft YaHei UI',10,'bold')).grid(row=0,column=0,sticky='w',padx=8,pady=(4,0))
        self.label(summary,variable=self.path_state,muted=True).grid(row=0,column=1,padx=8)
        self.meta_label=self.label(summary,variable=self.target_meta,muted=True,font=('Microsoft YaHei UI',9))
        self.meta_label.grid(row=1,column=0,columnspan=2,sticky='w',padx=8,pady=(0,4))
        summary.bind('<Configure>',lambda e:self.meta_label.configure(wraplength=max(200,e.width-20)))
        self.pane=ttk.Panedwindow(root,orient='horizontal')
        self.pane.grid(row=3,column=0,sticky='nsew')
        left,right=self.surface(self.pane),self.surface(self.pane)
        self.pane.add(left,weight=4)
        self.pane.add(right,weight=6)
        left.columnconfigure(0,weight=1)
        left.rowconfigure(1,weight=1)
        right.columnconfigure(0,weight=1)
        right.rowconfigure(1,weight=1)
        self.label(left,'控件层级 · 双击选择父级',font=('Microsoft YaHei UI',10,'bold')).grid(row=0,column=0,sticky='w',padx=7,pady=6)
        holder=self.surface(left)
        holder.grid(row=1,column=0,sticky='nsew',padx=5)
        holder.rowconfigure(0,weight=1)
        holder.columnconfigure(0,weight=1)
        self.hierarchy=ttk.Treeview(holder,columns=('type','id','class'),show='tree headings',selectmode='browse')
        for col,title,width in [('#0','名称',170),('type','类型',80),('id','Automation ID',150),('class','Class',100)]:
            self.hierarchy.heading(col,text=title)
            # Only the final column absorbs spare width; other columns retain user sizing.
            self.hierarchy.column(col,width=width,minwidth=60,stretch=(col=='class'))
        self.hierarchy.grid(row=0,column=0,sticky='nsew')
        self.add_scrollbars(holder,self.hierarchy)
        self.hierarchy.bind('<Double-1>',self.select_parent)
        # All six parameters/actions share one row. Horizontal scroll only when needed.
        settings=self.surface(left)
        settings.grid(row=2,column=0,sticky='ew',padx=5,pady=5)
        settings.columnconfigure(0,weight=1)
        self.settings_canvas=tk.Canvas(settings,height=38,bd=0,highlightthickness=0)
        self.settings_canvas.grid(row=0,column=0,sticky='ew')
        self.surfaces.append((self.settings_canvas,'card'))
        form=self.surface(self.settings_canvas)
        self.settings_canvas.create_window((0,0),window=form,anchor='nw')
        self.label(form,'范围',muted=True).grid(row=0,column=0,padx=(0,4))
        ttk.Combobox(form,textvariable=self.scope,values=('captured','top window'),state='readonly',width=10).grid(row=0,column=1)
        self.button(form,'读取树',self.read_tree,action='target').grid(row=0,column=2,padx=5)
        for index,(title,var,low,high,width) in enumerate([
            ('深度',self.depth,0,20,3),('节点',self.nodes,1,10000,5),('预算/秒',self.seconds,1,120,3)]):
            self.label(form,title,muted=True).grid(row=0,column=3+index*2,padx=(6,3))
            ttk.Spinbox(form,textvariable=var,from_=low,to=high,width=width).grid(row=0,column=4+index*2)
        def fit_form(event=None):
            self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox('all'),height=form.winfo_reqheight()+2)
        form.bind('<Configure>',fit_form)
        bar=SlimScrollbar(settings,self.settings_canvas.xview,orient='horizontal')
        bar.grid(row=1,column=0,sticky='ew')
        self.settings_canvas.configure(xscrollcommand=bar.set)
        self.scrollbars.append(bar)
        tabs=self.surface(right)
        tabs.grid(row=0,column=0,sticky='ew',padx=5,pady=5)
        for col,(name,title) in enumerate(TABS.items()):
            button=self.button(tabs,title,lambda key=name:self.switch_tab(key),kind='tab')
            button.grid(row=0,column=col,padx=(0,3))
            self.tab_buttons[name]=button
        content=self.surface(right)
        content.grid(row=1,column=0,sticky='nsew',padx=5,pady=(0,5))
        content.rowconfigure(0,weight=1)
        content.columnconfigure(0,weight=1)
        self.tabs={}
        for name in TABS:
            frame=self.surface(content)
            frame.grid(row=0,column=0,sticky='nsew')
            frame.rowconfigure(0,weight=1)
            frame.columnconfigure(0,weight=1)
            text=tk.Text(frame,wrap='none',font=('Consolas',11),bd=0,highlightthickness=0,padx=8,pady=7,state='disabled')
            text.grid(row=0,column=0,sticky='nsew')
            text.bind('<Control-a>',lambda e,w=text:self.select_all(w))
            self.add_scrollbars(frame,text)
            self.tabs[name],self.tab_frames[name]=text,frame
            self.text(name,'尚无数据。\n\n鼠标移到目标控件，按 Ctrl + Shift + F8 捕获。')
        footer=self.surface(root,'bg')
        footer.grid(row=4,column=0,sticky='ew',pady=(4,0))
        footer.columnconfigure(0,weight=1)
        self.status_label=self.label(footer,variable=self.status,bg='bg',font=('Microsoft YaHei UI',9))
        self.status_label.grid(row=0,column=0,sticky='w')
        self.label(footer,'Ctrl+Shift：F8 捕获 / F9 父级 / F10 暂停',muted=True,bg='bg',font=('Microsoft YaHei UI',8)).grid(row=0,column=1,padx=8)
        footer.bind('<Configure>',lambda e:self.status_label.configure(wraplength=max(200,e.width-380)))
        self.apply_theme()
        self.switch_tab('Summary')
        self.after_idle(lambda:self.pane.sashpos(0,int(self.pane.winfo_width()*.43)))
        self.after(120,self.refresh_actions)

    def surface(self,parent,role='card'):
        widget=tk.Frame(parent,bd=0)
        self.surfaces.append((widget,role))
        return widget

    def label(self,parent,text='',variable=None,muted=False,bg='card',**kw):
        widget=tk.Label(parent,text=text,textvariable=variable,anchor='w',bd=0,**kw)
        self.labels.append((widget,'muted' if muted else 'text',bg))
        return widget

    def button(self,parent,title,command,kind='normal',action='always'):
        widget=HoverButton(parent,text=title,command=command)
        self.action_buttons.append((widget,kind,action))
        return widget

    def add_scrollbars(self,parent,widget):
        vertical=SlimScrollbar(parent,widget.yview)
        horizontal=SlimScrollbar(parent,widget.xview,orient='horizontal')
        vertical.grid(row=0,column=1,sticky='ns')
        horizontal.grid(row=1,column=0,sticky='ew')
        widget.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.scrollbars.extend((vertical,horizontal))

    def apply_theme(self):
        c=THEMES[self.theme_name]
        self.configure(bg=c['bg'])
        for widget,role in self.surfaces: widget.configure(bg=c[role])
        for widget,fg,bg in self.labels: widget.configure(fg=c[fg],bg=c[bg])
        self.style.configure('.',font=('Microsoft YaHei UI',10),background=c['card'],foreground=c['text'])
        self.style.configure('TPanedwindow',background=c['bg'],sashwidth=7)
        self.style.configure('Treeview',background=c['card'],fieldbackground=c['card'],foreground=c['text'],rowheight=28,borderwidth=0)
        self.style.configure('Treeview.Heading',background=c['selected'],foreground=c['text'],font=('Microsoft YaHei UI',9,'bold'),padding=(6,6),relief='flat')
        self.style.map('Treeview',background=[('selected',c['selected'])],foreground=[('selected',c['text'])])
        self.style.map('Treeview.Heading',background=[('active',c['hover'])])
        for name in ('TCombobox','TSpinbox','TEntry'):
            self.style.configure(name,fieldbackground=c['card'],foreground=c['text'],background=c['card'],bordercolor=c['line'],arrowcolor=c['text'],padding=3)
            self.style.map(name,fieldbackground=[('readonly',c['card'])],foreground=[('readonly',c['text'])],selectbackground=[('readonly',c['selected'])],selectforeground=[('readonly',c['text'])])
        self.option_add('*TCombobox*Listbox.background',c['card'])
        self.option_add('*TCombobox*Listbox.foreground',c['text'])
        for text in self.tabs.values():
            text.configure(bg=c['editor'],fg=c['text'],insertbackground=c['text'],selectbackground=c['selected'],selectforeground=c['text'])
        for bar in self.scrollbars: bar.theme(c)
        for widget,kind,action in self.action_buttons:
            if kind=='primary': widget.palette(c['accent'],c['primaryhover'],'#FFFFFF','#C7D6EF')
            else: widget.palette(c['card'],c['hover'],c['text'],c['muted'])
        self.theme_button.configure(text='切换深色' if self.theme_name=='light' else '切换浅色')
        self.style_tabs()

    def style_tabs(self):
        c=THEMES[self.theme_name]
        for name,widget in self.tab_buttons.items():
            active=name==self.current_tab
            widget.palette(c['accent'] if active else c['card'],c['primaryhover'] if active else c['hover'],'#FFFFFF' if active else c['text'],c['muted'])

    def switch_tab(self,name):
        self.current_tab=name
        self.tab_frames[name].tkraise()
        self.style_tabs()

    def toggle_theme(self):
        self.theme_name='dark' if self.theme_name=='light' else 'light'
        self.apply_theme()

    def select_all(self,widget):
        widget.tag_add('sel','1.0','end-1c')
        return 'break'

    def text(self,name,value):
        widget=self.tabs[name]
        widget.configure(state='normal')
        widget.delete('1.0','end')
        widget.insert('1.0',value)
        widget.configure(state='disabled')

    def log(self,value):
        widget=self.tabs['Log']
        widget.configure(state='normal')
        widget.insert('end',f'[{datetime.now():%H:%M:%S}] {value}\n')
        if int(widget.index('end-1c').split('.')[0])>3000: widget.delete('1.0','501.0')
        widget.see('end')
        widget.configure(state='disabled')

    def refresh_actions(self):
        if self.closing: return
        for widget,kind,action in self.action_buttons:
            enabled=True
            if action=='capture': enabled=self.ready and not self.busy
            elif action=='target': enabled=self.snapshot is not None and not self.busy
            elif action=='cancel': enabled=self.busy or self.timer is not None
            state='normal' if enabled else 'disabled'
            if str(widget.cget('state'))!=state:
                widget.cleanup()
                widget.configure(state=state,bg=widget.base_color)
        self.after(120,self.refresh_actions)

    def show(self,snapshot):
        super().show(snapshot)
        data=snapshot['element']
        self.target_name.set(str(data.get('name') or data.get('automation_id') or '<未命名>')[:100])
        self.target_meta.set(f"类型 {data.get('control_type') or '?'}  /  Class {data.get('class_name') or '?'}  /  PID {data.get('process_id')}  /  {data['backend']}")
        self.path_state.set(f"{'路径完整' if snapshot['path_complete'] else '路径不完整'} · {len(snapshot['locator_path'])} 层")

    def event(self,kind,ident,value):
        super().event(kind,ident,value)
        if kind=='result' and ident==self.active:
            operation,result,elapsed=value
            if operation=='properties': self.switch_tab('All Properties')
            elif operation=='tree': self.switch_tab('Control Tree')
        if kind=='error': self.switch_tab('Log')

    def backend_changed(self,event=None):
        super().backend_changed(event)
        self.target_name.set('尚未捕获控件')
        self.target_meta.set('后端已切换，请重新捕获')
        self.path_state.set('等待捕获')

    def copy_code(self):
        super().copy_code()
        self.switch_tab('Generated Code')


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
