# -*- coding: utf-8 -*-
"""Windows desktop UI element inspector for automation development."""
from __future__ import annotations
import ctypes, json, queue, sys, threading, time, traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from pywinauto import Desktop
except ImportError:
    Desktop = None
try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None

APP = "Desktop UI Inspector"
VERSION = "1.0.0"
OUT_DIR = Path(__file__).resolve().parent / "captures"
IS_WINDOWS = sys.platform == "win32"
user32 = ctypes.windll.user32 if IS_WINDOWS else None
kernel32 = ctypes.windll.kernel32 if IS_WINDOWS else None
WM_HOTKEY, PM_REMOVE = 0x0312, 0x0001
MOD_CONTROL, MOD_SHIFT = 0x0002, 0x0004
HK_CAPTURE, HK_PARENT, HK_PAUSE = 1001, 1002, 1003
VK_F8, VK_F9, VK_F10 = 0x77, 0x78, 0x79

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", POINT)]

def safe(fn, default=""):
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default

def serial(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [serial(x) for x in value]
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    return str(value)

def cursor_pos():
    p = POINT()
    if not user32.GetCursorPos(ctypes.byref(p)):
        raise ctypes.WinError()
    return p.x, p.y

def rect_dict(r):
    if not r:
        return {}
    return {"left": int(r.left), "top": int(r.top), "right": int(r.right),
            "bottom": int(r.bottom), "width": int(r.right-r.left),
            "height": int(r.bottom-r.top)}

def clean(v):
    return str(v or "").replace("\r", " ").replace("\n", " ").strip()

def summary(w, backend):
    i = w.element_info
    return {
        "backend": backend,
        "name": clean(safe(lambda: i.name)),
        "automation_id": clean(safe(lambda: i.automation_id)),
        "control_type": clean(safe(lambda: i.control_type)),
        "class_name": clean(safe(lambda: i.class_name)),
        "framework_id": clean(safe(lambda: i.framework_id)),
        "process_id": safe(lambda: int(i.process_id), 0),
        "handle": safe(lambda: int(i.handle), 0),
        "rectangle": rect_dict(safe(lambda: i.rectangle, None)),
        "runtime_id": serial(safe(lambda: i.runtime_id, [])),
        "visible": bool(safe(lambda: w.is_visible(), False)),
        "enabled": bool(safe(lambda: w.is_enabled(), False)),
        "texts": serial(safe(lambda: w.texts(), [])),
    }

def all_properties(w):
    i, result = w.element_info, {}
    names = set(getattr(i, "__dict__", {}).keys())
    names.update(("name", "automation_id", "control_type", "class_name", "framework_id",
                  "process_id", "handle", "rectangle", "runtime_id", "rich_text",
                  "access_key", "accelerator", "legacy_name", "legacy_value"))
    for name in sorted(names):
        if name.startswith("_"):
            continue
        value = safe(lambda n=name: getattr(i, n), "<unavailable>")
        if not callable(value):
            result[name] = serial(value)
    result.update({"wrapper_class": type(w).__name__,
                   "window_text": safe(lambda: w.window_text()),
                   "friendly_class_name": safe(lambda: w.friendly_class_name()),
                   "is_visible": safe(lambda: w.is_visible(), False),
                   "is_enabled": safe(lambda: w.is_enabled(), False)})
    return result

def ancestors(w, backend, limit=30):
    items = []
    for _ in range(limit):
        if w is None:
            break
        items.append(summary(w, backend))
        w = safe(lambda: w.parent(), None)
    return list(reversed(items))

def exe_path(pid):
    if not pid:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)

def generated_code(d):
    args = []
    if d["backend"] == "uia" and d.get("automation_id"):
        args.append(f"auto_id={d['automation_id']!r}")
    if d.get("name"):
        args.append(f"title={d['name']!r}")
    if d["backend"] == "uia" and d.get("control_type"):
        args.append(f"control_type={d['control_type']!r}")
    elif d.get("class_name"):
        args.append(f"class_name={d['class_name']!r}")
    selector = ",\n    ".join(args) or "found_index=0"
    return ("from pywinauto import Desktop\n\n"
            f"desktop = Desktop(backend={d['backend']!r})\n"
            f"window = desktop.window(process={d['process_id']})\n"
            f"element = window.child_window(\n    {selector}\n)\n"
            "element.wait('exists enabled visible ready', timeout=10)\n"
            "element.click_input()\n")

def walk_tree(root, backend, max_depth=6, max_nodes=1000):
    lines, count = [], [0]
    def walk(w, depth):
        if count[0] >= max_nodes:
            return
        count[0] += 1
        d = summary(w, backend)
        lines.append("    "*depth + f"[{d.get('control_type') or '?'}] "
                     f"{d.get('name') or '<unnamed>'} (auto_id={d.get('automation_id')!r}, "
                     f"class={d.get('class_name')!r})")
        if depth < max_depth:
            for child in safe(lambda: w.children(), []):
                walk(child, depth+1)
    walk(root, 0)
    return "\n".join(lines)

class Hotkeys(threading.Thread):
    def __init__(self, events):
        super().__init__(daemon=True); self.events = events; self.stop_flag = threading.Event()
    def run(self):
        specs = ((HK_CAPTURE, VK_F8, "capture"), (HK_PARENT, VK_F9, "parent"),
                 (HK_PAUSE, VK_F10, "pause"))
        registered = []
        for ident, vk, _ in specs:
            if user32.RegisterHotKey(None, ident, MOD_CONTROL | MOD_SHIFT, vk):
                registered.append(ident)
            else:
                self.events.put(("error", f"Global hotkey registration failed: {ident}"))
        msg = MSG()
        mapping = {x[0]: x[2] for x in specs}
        try:
            while not self.stop_flag.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY and msg.wParam in mapping:
                        self.events.put(("hotkey", mapping[msg.wParam]))
                time.sleep(.03)
        finally:
            for ident in registered:
                user32.UnregisterHotKey(None, ident)
    def stop(self): self.stop_flag.set()

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} {VERSION}"); self.geometry("1180x760"); self.minsize(950, 620)
        self.events, self.lock = queue.Queue(), threading.Lock()
        self.wrapper = self.data = None; self.paused = False
        OUT_DIR.mkdir(exist_ok=True)
        self.build_ui()
        self.hotkeys = Hotkeys(self.events); self.hotkeys.start()
        self.after(80, self.poll); self.protocol("WM_DELETE_WINDOW", self.close)
        self.status.set("Ready. Point at a control and press Ctrl+Shift+F8.")
    def build_ui(self):
        bar = ttk.Frame(self, padding=8); bar.pack(fill="x")
        ttk.Label(bar, text="Backend:").pack(side="left")
        self.backend = tk.StringVar(value="uia")
        ttk.Combobox(bar, textvariable=self.backend, values=("uia", "win32"),
                     state="readonly", width=8).pack(side="left", padx=(4,12))
        for text, cmd in (("Capture (F8)", lambda: self.capture(False)),
                          ("Capture Parent (F9)", lambda: self.capture(True)),
                          ("Highlight", self.highlight), ("Screenshot", self.screenshot),
                          ("Save JSON", self.save_json), ("Refresh Tree", self.refresh_tree)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=3)
        pane = ttk.Panedwindow(self, orient="horizontal"); pane.pack(fill="both", expand=True, padx=8)
        left, right = ttk.Frame(pane), ttk.Frame(pane); pane.add(left, weight=2); pane.add(right, weight=3)
        ttk.Label(left, text="Element hierarchy").pack(anchor="w")
        self.hierarchy = ttk.Treeview(left, columns=("type","id","class"), show="tree headings")
        for col, title, width in (("#0","Name",210),("type","Control Type",110),
                                  ("id","Automation ID",150),("class","Class",150)):
            self.hierarchy.heading(col, text=title); self.hierarchy.column(col, width=width)
        sy = ttk.Scrollbar(left, command=self.hierarchy.yview); self.hierarchy.configure(yscrollcommand=sy.set)
        self.hierarchy.pack(side="left", fill="both", expand=True); sy.pack(side="right", fill="y")
        book = ttk.Notebook(right); book.pack(fill="both", expand=True)
        self.summary = self.text_tab(book, "Summary"); self.props = self.text_tab(book, "All Properties")
        self.code = self.text_tab(book, "Generated Code"); self.tree = self.text_tab(book, "Control Tree")
        self.log = self.text_tab(book, "Log")
        foot = ttk.Frame(self, padding=8); foot.pack(fill="x")
        self.status = tk.StringVar(); ttk.Label(foot, textvariable=self.status).pack(side="left")
        ttk.Label(foot, text="Ctrl+Shift+F10: pause/resume").pack(side="right")
    def text_tab(self, book, title):
        f = ttk.Frame(book); t = tk.Text(f, wrap="none", font=("Consolas",10))
        sy, sx = ttk.Scrollbar(f, command=t.yview), ttk.Scrollbar(f, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=sy.set, xscrollcommand=sx.set); t.grid(row=0,column=0,sticky="nsew")
        sy.grid(row=0,column=1,sticky="ns"); sx.grid(row=1,column=0,sticky="ew")
        f.rowconfigure(0,weight=1); f.columnconfigure(0,weight=1); book.add(f,text=title); return t
    def set_text(self, widget, value):
        widget.delete("1.0","end"); widget.insert("1.0", value)
    def write_log(self, value):
        self.log.insert("end", f"[{datetime.now():%H:%M:%S}] {value}\n"); self.log.see("end")
    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "hotkey":
                    if value == "pause": self.paused = not self.paused; self.status.set("Hotkeys paused." if self.paused else "Hotkeys resumed.")
                    elif not self.paused: self.capture(value == "parent")
                elif kind == "result": self.show_result(*value)
                elif kind == "tree": self.set_text(self.tree, value); self.status.set("Control tree refreshed.")
                elif kind == "error": self.write_log(value); self.status.set(value.splitlines()[0])
        except queue.Empty: pass
        self.after(80, self.poll)
    def capture(self, parent=False):
        if Desktop is None:
            messagebox.showerror(APP, "Missing pywinauto. Run: pip install pywinauto Pillow pywin32"); return
        if not self.lock.acquire(False): return
        backend = self.backend.get(); self.status.set("Capturing...")
        def work():
            try:
                x,y = cursor_pos(); w = Desktop(backend=backend).from_point(x,y)
                if parent: w = safe(lambda: w.parent(), w)
                d = summary(w, backend); d.update({"cursor":{"x":x,"y":y},
                    "captured_at":datetime.now().isoformat(timespec="seconds")})
                d["executable"] = exe_path(d["process_id"])
                self.events.put(("result", (w,d,all_properties(w),ancestors(w,backend))))
            except Exception as e: self.events.put(("error", f"Capture failed: {e}\n{traceback.format_exc()}"))
            finally: self.lock.release()
        threading.Thread(target=work, daemon=True).start()
    def show_result(self, w, d, props, chain):
        self.wrapper = w; self.data = {"element":d,"properties":props,"ancestors":chain}
        self.set_text(self.summary, json.dumps(d,ensure_ascii=False,indent=2))
        self.set_text(self.props, json.dumps(props,ensure_ascii=False,indent=2))
        self.set_text(self.code, generated_code(d)); self.hierarchy.delete(*self.hierarchy.get_children())
        parent = ""
        for item in chain:
            parent = self.hierarchy.insert(parent,"end",text=item.get("name") or "<unnamed>",
                values=(item.get("control_type"),item.get("automation_id"),item.get("class_name")),open=True)
        self.status.set(f"Captured: {d.get('control_type')} | {d.get('name') or '<unnamed>'}")
        self.write_log(f"Captured {d['backend']} at ({d['cursor']['x']}, {d['cursor']['y']})"); self.highlight()
    def highlight(self):
        if not self.data: return
        r = self.data["element"]["rectangle"]
        if not r or r["width"] < 1 or r["height"] < 1: return
        o = tk.Toplevel(self); o.overrideredirect(True); o.attributes("-topmost",True); o.attributes("-transparentcolor","white")
        o.geometry(f"{r['width']}x{r['height']}+{r['left']}+{r['top']}")
        c = tk.Canvas(o,bg="white",highlightthickness=0); c.pack(fill="both",expand=True)
        c.create_rectangle(2,2,max(3,r['width']-3),max(3,r['height']-3),outline="red",width=4); o.after(900,o.destroy)
    def refresh_tree(self):
        if not self.wrapper: messagebox.showinfo(APP,"Capture an element first."); return
        current, backend = self.wrapper, self.backend.get(); self.status.set("Reading control tree...")
        def work():
            try:
                root = current
                for _ in range(50):
                    p = safe(lambda: root.parent(), None)
                    if p is None: break
                    root = p
                self.events.put(("tree",walk_tree(root,backend)))
            except Exception as e: self.events.put(("error",f"Tree read failed: {e}"))
        threading.Thread(target=work,daemon=True).start()
    def save_json(self):
        if not self.data: messagebox.showinfo(APP,"Capture an element first."); return
        name = f"element_{datetime.now():%Y%m%d_%H%M%S}.json"
        fn = filedialog.asksaveasfilename(initialdir=str(OUT_DIR),initialfile=name,defaultextension=".json",filetypes=[("JSON","*.json")])
        if fn: Path(fn).write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding="utf-8"); self.status.set(f"Saved: {fn}")
    def screenshot(self):
        if not self.data: messagebox.showinfo(APP,"Capture an element first."); return
        if ImageGrab is None: messagebox.showerror(APP,"Missing Pillow. Run: pip install Pillow"); return
        r = self.data["element"]["rectangle"]; name=f"element_{datetime.now():%Y%m%d_%H%M%S}.png"
        fn=filedialog.asksaveasfilename(initialdir=str(OUT_DIR),initialfile=name,defaultextension=".png",filetypes=[("PNG","*.png")])
        if fn: ImageGrab.grab(bbox=(r["left"],r["top"],r["right"],r["bottom"]),all_screens=True).save(fn); self.status.set(f"Saved: {fn}")
    def close(self): self.hotkeys.stop(); self.destroy()

def main():
    if not IS_WINDOWS: print("This tool supports Windows only."); return 1
    try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception: pass
    if Desktop is None or ImageGrab is None:
        root=tk.Tk(); root.withdraw(); messagebox.showerror(APP,"Install dependencies:\npython -m pip install pywinauto Pillow pywin32"); return 2
    App().mainloop(); return 0
if __name__ == "__main__": raise SystemExit(main())
