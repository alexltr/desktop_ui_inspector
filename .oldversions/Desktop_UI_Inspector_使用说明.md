# Desktop UI Inspector 使用说明

## 1. 简介

`Desktop UI Inspector` 是一个用于 Windows 桌面自动化开发的通用元素侦察工具。它基于 `pywinauto`，支持 UIA 和 Win32 两种后端，适用于 ERP、PLM、MES、WinForms、WPF、Qt、Electron 及传统 Win32 程序。

## 2. 文件

- `desktop_ui_inspector.py`：完整程序
- `Desktop_UI_Inspector_使用说明.md`：本文档
- `captures`：运行后自动创建，建议用于保存截图和 JSON

## 3. 功能

- 全局热键捕获鼠标所在元素
- 捕获当前元素或其父元素
- 切换 UIA / Win32 后端
- 查看 Name、AutomationId、ControlType、ClassName、HWND、PID、RuntimeId、Rectangle 等属性
- 查看完整父级路径
- 读取窗口控件树
- 红框高亮元素
- 保存元素截图和 JSON
- 自动生成 `pywinauto` 定位代码
- 支持高 DPI 和多显示器截图

## 4. 环境要求

- Windows 10/11
- Python 3.9+
- 工具和目标应用尽量保持相同权限级别

目标程序若以管理员身份运行，本工具也应以管理员身份运行。

## 5. 安装

```bash
python -m pip install --upgrade pip
python -m pip install pywinauto Pillow pywin32
```

## 6. 启动

```bash
python desktop_ui_inspector.py
```

## 7. 快速操作

1. 启动工具和目标程序。
2. 默认使用 `uia` 后端。
3. 把鼠标停在目标控件上。
4. 按 `Ctrl+Shift+F8` 捕获元素。
5. 查看 Summary、All Properties 和 Generated Code。
6. 若元素层级太细，按 `Ctrl+Shift+F9` 捕获父元素。
7. UIA 无法识别时切换为 `win32` 后重试。

## 8. 热键

- `Ctrl+Shift+F8`：捕获鼠标所在元素
- `Ctrl+Shift+F9`：捕获其父元素
- `Ctrl+Shift+F10`：暂停或恢复热键

热键被其他软件占用时，仍可使用窗口按钮。

## 9. 标签页

### Summary

显示常用元素信息，包括名称、AutomationId、ControlType、ClassName、PID、HWND、矩形范围、可见性、可用状态及可执行文件路径。

### All Properties

显示 `element_info` 暴露的更多底层属性。不同 UI 框架提供的属性数量会不同。

### Generated Code

生成基础的 `pywinauto` 代码。生成结果是定位起点，正式自动化代码中应验证定位是否唯一，并尽量使用稳定的顶层窗口条件。

### Control Tree

点击 `Refresh Tree` 后，从目标元素向上找到根节点，再读取控件树。为防止大型应用卡顿，默认最多读取 6 层和 1000 个节点。

### Log

记录捕获坐标、后端和错误信息。

## 10. 定位器选择建议

建议优先级：

1. `automation_id`
2. `control_type` + `name`
3. 稳定父级 + 子元素条件
4. `class_name` + `name`
5. `found_index`
6. 坐标或图像识别

不要把 HWND 或 PID 作为长期稳定条件，它们通常会在程序重启后变化。

示例：

```python
from pywinauto import Desktop

desktop = Desktop(backend="uia")
window = desktop.window(title_re=".*ERP.*")
element = window.child_window(
    auto_id="btnExport",
    control_type="Button"
)
element.wait("exists enabled visible ready", timeout=10)
element.click_input()
```

## 11. UIA 与 Win32 的选择

### UIA

优先用于 WPF、UWP/WinUI、新版 WinForms、Electron，以及公开 Accessibility 信息的 Qt 程序。UIA 通常能提供 AutomationId 和 ControlType。

### Win32

适合传统 Win32、MFC、老版本 ERP 和标准 Windows 原生控件。它更依赖 HWND、ClassName、标题与控件层级。

## 12. 常见问题

### 只能捕获整个窗口

可能是自绘界面、DirectX、Citrix/远程画面，或目标程序未公开 UIA 信息。可依次尝试：

1. 以管理员身份运行本工具。
2. 切换 UIA / Win32。
3. 使用键盘 Tab 检查是否存在可访问元素。
4. 使用 Microsoft Inspect 或 Accessibility Insights 交叉验证。
5. 无内部元素时改用 OCR、图像识别、键盘导航或坐标方案。

### 红框位置偏移

程序会尝试启用 Per-Monitor DPI Awareness。若仍偏移，请保持各显示器缩放比例稳定，并重新启动工具。

### 控件树读取很慢

大型 ERP、浏览器或 Electron 应用可能包含大量节点。可捕获更接近目标区域的父容器，或在源码中降低 `max_depth` 和 `max_nodes`。

### 热键无反应

确认热键未被其他软件占用、状态栏未显示暂停，并检查工具与目标程序权限是否一致。

## 13. 打包 EXE

```bash
python -m pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name DesktopUIInspector desktop_ui_inspector.py
```

输出文件位于：

```text
dist\DesktopUIInspector.exe
```

## 14. 开发注意事项

- 关键动作前使用 `wait()`，不要只依赖固定 `sleep()`。
- 保存、删除、提交等操作应增加结果验证。
- 优先选择稳定属性，不要只依赖坐标或 `found_index`。
- 自绘界面没有公开内部元素时，本工具无法凭空获取控件语义。
