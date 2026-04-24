import ctypes
import time
import json
import os
import sys
import threading
import winreg
import subprocess
import tkinter as tk
from tkinter import ttk
from datetime import datetime

# 托盘所需库
import pystray
from PIL import Image, ImageDraw

# --- 1. 基础环境配置 ---
# 智能获取当前运行目录
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

DATA_FILE = os.path.join(application_path, "screen_time_data.json")
APP_NAME = "MyScreenTimeLogger"

# 初始化底层 DLL
kernel32 = ctypes.windll.kernel32
version = ctypes.windll.version
user32 = ctypes.windll.user32
running = True

# ==========================================
#          模块 A：数据采集与托盘后台
# ==========================================

# 定义读取版本信息所需的 C 语言结构体
class LANGANDCODEPAGE(ctypes.Structure):
    _fields_ = [
        ("wLanguage", ctypes.c_uint16),
        ("wCodePage", ctypes.c_uint16)
    ]

def get_window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    return None

def get_exe_path(hwnd):
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
    h_process = kernel32.OpenProcess(0x1000, False, pid)
    if not h_process: return None
    
    exe_path = ctypes.create_unicode_buffer(260)
    size = ctypes.c_ulong(260)
    success = kernel32.QueryFullProcessImageNameW(h_process, 0, exe_path, ctypes.byref(size))
    kernel32.CloseHandle(h_process)
    return exe_path.value if success else None

def get_file_description(exe_path):
    """智能遍历多语言属性，优先提取中文软件真名"""
    if not exe_path: return None
    try:
        size = version.GetFileVersionInfoSizeW(exe_path, None)
        if not size: return None

        res = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(exe_path, 0, size, res): return None

        buffer_len = ctypes.c_uint()
        buffer_ptr = ctypes.c_void_p()
        if not version.VerQueryValueW(res, r'\VarFileInfo\Translation', ctypes.byref(buffer_ptr), ctypes.byref(buffer_len)):
            return None
        
        if buffer_len.value == 0: return None
        
        # 1. 计算该 exe 内部包含了多少种语言的描述
        num_translations = buffer_len.value // ctypes.sizeof(LANGANDCODEPAGE)
        translations = ctypes.cast(buffer_ptr, ctypes.POINTER(LANGANDCODEPAGE))

        preferred_blocks = [] # 优先语言 (中文)
        other_blocks = []     # 备用语言 (英文及其他)

        # 2. 对所有语言块进行分类
        for i in range(num_translations):
            lang = translations[i].wLanguage
            codepage = translations[i].wCodePage
            block_name = f"{lang:04x}{codepage:04x}"
            
            # 0x0804 是简体中文，0x0404 是繁体中文
            if lang in (0x0804, 0x0404):
                preferred_blocks.append(block_name)
            else:
                other_blocks.append(block_name)

        # 把中文排在最前面查找
        search_blocks = preferred_blocks + other_blocks

        # 3. 开始在语言块中挖取信息 (先找 FileDescription，找不到再找 ProductName)
        for block in search_blocks:
            for key in ["FileDescription", "ProductName"]:
                sub_block = rf'\StringFileInfo\{block}\{key}'
                
                if version.VerQueryValueW(res, sub_block, ctypes.byref(buffer_ptr), ctypes.byref(buffer_len)):
                    if buffer_len.value > 0:
                        description = ctypes.cast(buffer_ptr, ctypes.c_wchar_p).value
                        if description and description.strip():
                            return description.strip()
    except Exception:
        pass
    return None

def get_active_app_name():
    """获取过滤后的纯净软件名"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd: return None

    full_title = get_window_title(hwnd)
    if not full_title: return None

    if full_title == "Program Manager":
        return "桌面"
    
    # 步骤 1：优先透视获取底层 exe 官方真名 (完美解决网易云、浏览器标题乱变)
    exe_path = get_exe_path(hwnd)
    full_title = get_file_description(exe_path)
    
    # 过滤系统噪音
    system_ui_keywords = [
        "系统托盘溢出窗口", "任务栏", "开始", "搜索", "操作中心", 
        "任务视图", "小组件", "新通知", "网络连接",
        "Taskbar", "Start", "Search", "Action Center", "Task View", "Widgets", "Program Manager", "New Notification", "Network Connections","快速设置",
        "Windows Shell Experience 主机", "Windows Shell Experience Host",
        "Unknown", "截图工具覆盖"
    ]
    for keyword in system_ui_keywords:
        if keyword == full_title or (keyword + "。") in full_title:
            return "系统界面"
        
    # 智能切割提取软件名
    if " - " in full_title:
        return full_title.split(" - ")[-1].strip()
    return full_title

def monitor_loop():
    """后台监控循环"""
    global running
    
    # 读取旧数据
    data = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            try: data = json.load(f)
            except: pass
            
    while running:
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in data: data[today] = {}
            
        active_app = get_active_app_name()
        if active_app:
            if active_app not in data[today]: data[today][active_app] = 0
            data[today][active_app] += 5
            
        # 写入文件
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        
        time.sleep(5)

# --- 托盘菜单功能 ---
def check_autostart():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False

def toggle_autostart(icon, item):
    is_enabled = check_autostart()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        if is_enabled:
            winreg.DeleteValue(key, APP_NAME)
        else:
            # 写入当前运行的程序路径作为自启项
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, sys.executable)
        winreg.CloseKey(key)
    except Exception as e:
        print(f"自启设置失败: {e}")

def open_ui(icon, item):
    """【核心魔法】召唤分身打开界面"""
    if getattr(sys, 'frozen', False):
        # 如果是打包后的 exe，让 exe 带上 --ui 参数再次运行自己
        subprocess.Popen([sys.executable, "--ui"])
    else:
        # 如果是在编辑器里测试的 py 脚本
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--ui"])

def quit_app(icon, item):
    global running
    running = False
    icon.stop()

def run_logger():
    """启动托盘监控模式"""
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    
    # 读取本地的 traylogo.ico 作为托盘图标
    def get_icon_path():
        """获取图标绝对路径（兼容 PyInstaller 打包后的环境）"""
        if getattr(sys, 'frozen', False):
            # 如果是打包运行，读取隐藏的临时解压目录
            return os.path.join(sys._MEIPASS, "traylogo.ico")
        else:
            # 如果是代码运行，读取当前目录
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "traylogo.ico")

    icon_path = get_icon_path()
    try:
        if os.path.exists(icon_path):
            # 如果找到了图片，直接加载它
            image = Image.open(icon_path).convert("RGBA")
        else:
            with open("error_log.txt","a") as f: f.write(f"图片未找到：{icon_path}\n")
                                                         
    except Exception as e:
        print(f"加载图标失败: {e}")
        # 【兜底机制】：万一图片丢了，画个蓝色方块防止程序崩溃
        image = Image.new('RGBA', (64, 64), color=(0, 120, 215, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 16, 48, 48], outline="white", width=4)
    
    menu = pystray.Menu(
        pystray.MenuItem('打开用户界面', open_ui),
        pystray.MenuItem('开机自启动', toggle_autostart, checked=lambda item: check_autostart()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('退出', quit_app)
    )
    icon = pystray.Icon("ScreenTime", image, "屏幕时间监控", menu)
    icon.run()

# ==========================================
#          模块 B：用户界面 (UI) 与数据分析
# ==========================================
from datetime import timedelta
import flet as ft

# --- 1. 分类映射表 ---
# 定义配置文件的路径 (放在 exe 同目录下)
CONFIG_FILE = os.path.join(application_path, "category_config.json")

DEFAULT_CATEGORY_MAP = {
    "Microsoft Edge": "网页浏览",
    "Google Chrome": "网页浏览",
    "Visual Studio Code": "编程开发",
    "Cursor": "编程开发",
    "微信": "通讯社交",
    "QQ": "通讯社交",
    "Word": "办公学习",
    "Excel": "办公学习",
    "PowerPoint": "办公学习",
    "Bilibili": "影音娱乐",
    "网易云音乐": "影音娱乐",
    "系统界面": "系统与杂项",
    "Task Manager": "系统与杂项",
    "Windows Explorer": "系统与杂项"
}

# 放弃容易报错的 ft.Colors 枚举，全面使用最底层的十六进制颜色，绝对安全！
CATEGORY_COLORS = {
    "网页浏览": "#2196F3",     # 蓝色
    "编程开发": "#673AB7",     # 紫色
    "通讯社交": "#4CAF50",     # 绿色
    "办公学习": "#FF9800",     # 橙色
    "影音娱乐": "#E91E63",     # 粉色
    "系统与杂项": "#9E9E9E",   # 灰色
    "其他": "#607D8B"          # 蓝灰
}

def load_category_map():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CATEGORY_MAP, f, ensure_ascii=False, indent=4)
        return DEFAULT_CATEGORY_MAP
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        try: return json.load(f)
        except: return DEFAULT_CATEGORY_MAP

CURRENT_CATEGORY_MAP = load_category_map()

def get_app_category(app_name):
    if app_name in CURRENT_CATEGORY_MAP: return CURRENT_CATEGORY_MAP[app_name]
    for key, category in CURRENT_CATEGORY_MAP.items():
        if key.lower() in app_name.lower(): return category
    return "其他"

def load_all_data():
    if not os.path.exists(DATA_FILE): return {}
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        try: return json.load(f)
        except: return {}

# 新增：获取自记录以来的所有历史总数据
def get_historical_data():
    data = load_all_data()
    historical_app_data = {}
    for date, daily_data in data.items():
        for app, sec in daily_data.items():
            historical_app_data[app] = historical_app_data.get(app, 0) + sec
    return historical_app_data

def format_time(seconds):
    if seconds < 60:
        return "<1 分钟"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

# --- Flet 核心界面渲染 ---
def run_viewer():
    def main(page: ft.Page):
        page.title = "屏幕使用时间"
        page.window.width = 850
        page.window.height = 750
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.padding = 0
        page.theme = ft.Theme(
            font_family="DengXian, Segoe UI, Microsoft YaHei UI, sans-serif"
        )

        # 【核心终极修复：使用最原生的 Python 字典做状态管理，绝不报错！】
        app_state = {
            "view_date": datetime.now()
        }

        # --- 页面 1：单日记录 (支持日期切换) ---
        def get_today_view():
            data = load_all_data()
            
            # 直接从我们的字典里读日期
            target_date = app_state["view_date"]
            date_str = target_date.strftime("%Y-%m-%d")
            
            # 判断是否是真正的今天
            is_real_today = date_str == datetime.now().strftime("%Y-%m-%d")
            
            day_data = data.get(date_str, {})
            total_sec = sum(day_data.values())
            
            lv = ft.ListView(expand=True, spacing=5, padding=30)
            
            # 切换日期的逻辑函数
            def change_date(delta_days):
                # 直接修改字典里的日期，然后重新渲染
                app_state["view_date"] += timedelta(days=delta_days)
                main_content.content = get_today_view()
                page.update()

            # 1. 带有左右箭头的顶部标题栏
            header_row = ft.Row([
                ft.Text("今日屏幕时间" if is_real_today else f"{date_str} 屏幕时间", size=16, color="#757575"),
                ft.Row([
                    ft.IconButton(icon=ft.Icons.CHEVRON_LEFT, on_click=lambda _: change_date(-1), tooltip="前一天"),
                    # 如果是今天，禁用向右的箭头，防止查看未来的空数据
                    ft.IconButton(icon=ft.Icons.CHEVRON_RIGHT, on_click=lambda _: change_date(1), disabled=is_real_today, tooltip="后一天"),
                ], spacing=0)
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            
            lv.controls.append(header_row)
            lv.controls.append(ft.Text(format_time(total_sec) if total_sec > 0 else "0m", size=36, weight="bold"))
            lv.controls.append(ft.Container(height=15))

            if total_sec > 0:
                cat_summary = {}
                for app, sec in day_data.items():
                    cat = get_app_category(app)
                    cat_summary[cat] = cat_summary.get(cat, 0) + sec
                sorted_cats = sorted(cat_summary.items(), key=lambda x: x[1], reverse=True)

                segments = []
                for cat, sec in sorted_cats:
                    color = CATEGORY_COLORS.get(cat, "#607D8B")
                    segment_col = ft.Column(
                        controls=[
                            ft.Container(bgcolor=color, height=18, border_radius=4),
                            ft.Container(
                                content=ft.Text(cat, size=11, color="#757575", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.LEFT),
                                alignment=ft.Alignment(-1, 0)
                            )
                        ],
                        expand=max(1, int(sec)), 
                        spacing=4,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH 
                    )
                    segments.append(segment_col)
                
                lv.controls.append(ft.Container(content=ft.Row(controls=segments, spacing=2), padding=ft.padding.only(bottom=20)))
                lv.controls.append(ft.Divider(height=1, color="#EEEEEE"))
                lv.controls.append(ft.Container(height=10))
                lv.controls.append(ft.Text("应用使用情况", size=18, weight="bold"))

                sorted_apps = sorted(day_data.items(), key=lambda x: x[1], reverse=True)
                for app_name, sec in sorted_apps:
                    cat = get_app_category(app_name)
                    color = CATEGORY_COLORS.get(cat, "#607D8B")
                    lv.controls.append(
                        ft.ListTile(
                            leading=ft.CircleAvatar(content=ft.Text(app_name[:1], color="white", size=14), bgcolor=color, radius=18),
                            title=ft.Text(app_name, size=15, weight="bold"),
                            subtitle=ft.Text(cat, size=12, color="#757575"),
                            trailing=ft.Text(format_time(sec), size=14, weight="bold"),
                            content_padding=0 
                        )
                    )
            else:
                lv.controls.append(ft.Container(height=40))
                lv.controls.append(ft.Text("这天没有任何使用记录哦~", size=14, color="#9E9E9E", text_align=ft.TextAlign.CENTER))

            return lv

        # --- 页面 2：历史总计 ---
        def get_total_view():
            historical_data = get_historical_data()
            total_sec = sum(historical_data.values())
            lv = ft.ListView(expand=True, spacing=5, padding=30)
            lv.controls.append(ft.Text("总累计使用时间", size=16, color="#757575"))
            lv.controls.append(ft.Text(format_time(total_sec), size=36, weight="bold"))
            lv.controls.append(ft.Container(height=20))
            
            if total_sec > 0:
                lv.controls.append(ft.Text("历史所有应用记录", size=18, weight="bold"))
                lv.controls.append(ft.Container(height=5))
                sorted_apps = sorted(historical_data.items(), key=lambda x: x[1], reverse=True)
                for app_name, sec in sorted_apps:
                    cat = get_app_category(app_name)
                    color = CATEGORY_COLORS.get(cat, "#607D8B")
                    lv.controls.append(ft.ListTile(
                        leading=ft.CircleAvatar(content=ft.Text(app_name[:1], color="white", size=14), bgcolor=color, radius=18),
                        title=ft.Text(app_name, size=15, weight="bold"),
                        subtitle=ft.Text(cat, size=12, color="#757575"),
                        trailing=ft.Text(format_time(sec), size=14, weight="bold"),
                        content_padding=0
                    ))
            else:
                lv.controls.append(ft.Text("目前还没有任何历史数据", size=14, color="#757575"))
            return lv

        # --- 页面 3：七日趋势 ---
        def get_trend_view():
            data = load_all_data()
            days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]

            max_sec = 1 
            daily_stats = []

            # 统计每日的总时间和分类数据
            for date_str in days:
                daily_data = data.get(date_str, {})
                cat_times = {}
                day_total_sec = 0
                for app, sec in daily_data.items():
                    cat = get_app_category(app)
                    cat_times[cat] = cat_times.get(cat, 0) + sec
                    day_total_sec += sec
                
                max_sec = max(max_sec, day_total_sec)
                daily_stats.append((date_str, cat_times, day_total_sec))

            bars = []
            # 开始用 Container 搭建图表
            for i, (date_str, cat_times, day_total) in enumerate(daily_stats):
                day_col_controls = []
                
                # 1. 顶部留白：利用 max_sec 减去当天时间，占位把柱子往下压
                if max_sec > day_total:
                    day_col_controls.append(ft.Container(expand=max_sec - day_total))

                # 2. 从上到下绘制彩色柱子（反向遍历分类，让同一类始终在同一个高度层）
                for cat in reversed(list(CATEGORY_COLORS.keys())):
                    sec = cat_times.get(cat, 0)
                    if sec > 0:
                        day_col_controls.append(
                            ft.Container(
                                bgcolor=CATEGORY_COLORS.get(cat, "#607D8B"),
                                expand=sec,  # 时间秒数直接作为高度权重
                                width=35,
                                tooltip=f"{cat}\n{format_time(sec)}"
                            )
                        )

                # 3. 兜底空柱子
                if day_total == 0:
                    day_col_controls.append(ft.Container(bgcolor="#EEEEEE", height=4, width=35, border_radius=2))

                # 组合单日的柱子并加上圆角
                chart_bar = ft.Container(
                    content=ft.Column(controls=day_col_controls, spacing=0),
                    border_radius=4,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    expand=True
                )

                # 组合日期标签
                col_wrap = ft.Column(
                    controls=[
                        ft.Container(content=chart_bar, expand=True, alignment=ft.Alignment(0, 1)),
                        ft.Container(height=5),
                        ft.Text("今天" if i==6 else date_str[-5:], size=12, color="#757575", text_align=ft.TextAlign.CENTER)
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    expand=True
                )
                bars.append(col_wrap)

            # 横向摆放 7 天的柱子
            chart_row = ft.Row(controls=bars, alignment=ft.MainAxisAlignment.SPACE_AROUND, expand=True)

            return ft.Column([
                ft.Container(
                    content=ft.Row([
                        ft.Text("近七日屏幕趋势", size=24, weight="bold"),
                        ft.Text(f"最高记录: {format_time(max_sec)}", size=14, color="#757575")
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    padding=ft.padding.only(left=30, top=30, right=30)
                ),
                ft.Container(chart_row, expand=True, padding=ft.padding.only(left=10, right=10, bottom=30, top=20))
            ], expand=True)

        main_content = ft.Container(expand=True)

        def on_nav_change(e):
            idx = nav_rail.selected_index if e is None else e.control.selected_index
            
            # 点击左侧导航“今日”时，字典里的时间重置回现在
            if idx == 0:
                app_state["view_date"] = datetime.now()
            
            main_content.content = None 
            if idx == 0: main_content.content = get_today_view()
            elif idx == 1: main_content.content = get_total_view()
            elif idx == 2: main_content.content = get_trend_view()
            page.update() 

        nav_rail = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            destinations=[
                ft.NavigationRailDestination(icon=ft.Icons.TODAY, label="今日"),
                ft.NavigationRailDestination(icon=ft.Icons.HISTORY, label="总计"),
                ft.NavigationRailDestination(icon=ft.Icons.BAR_CHART, label="趋势"),
            ],
            on_change=on_nav_change,
        )

        page.floating_action_button = ft.FloatingActionButton(icon=ft.Icons.REFRESH, on_click=lambda _: on_nav_change(None))
        
        # 初始化显示第一页
        main_content.content = get_today_view()
        page.add(ft.Row([nav_rail, ft.VerticalDivider(width=1), main_content], expand=True))

    ft.app(target=main)

# ==========================================
#          程序总入口 (路由器)
# ==========================================
if __name__ == "__main__":
    # 检查启动参数中是否包含 --ui
    if len(sys.argv) > 1 and sys.argv[1] == "--ui":
        # 运行分身：展示用户界面
        run_viewer()
    else:
        # 正常双击运行：启动后台托盘监控
        run_logger()