# -*- coding: utf-8 -*-
"""AWB 控制台 —— agent-workspace-bridge 的图形界面。

Tab 1  技能分配矩阵：行 = 技能，列 = 全局 + 各工作空间，交叉处打勾启用。
       勾选结果写进 skills-matrix.json，CLI 脚本与共享层都以它为准。
Tab 2  共享工作空间：建/刷新 canonical、状态、落盘即见、漂移检查、快照、恢复、溯源。
Tab 3  跨 agent 投影：把专属技能铺到各家项目级目录（预览 / 应用）。
Tab 4  规则同步：snippet 块分发到 16 个客户端规则文件。
Tab 5  维护：取消隔离、清理残留目录、打开配置与日志、关于。

所有实际操作都交给同目录的 CLI 脚本执行（单一实现），界面只负责编辑事实源、
拼命令、显示输出。命令行入口：
    python awb_gui.py                    打开窗口
    python awb_gui.py --selftest        无窗口自检，失败返回非 0
    python awb_gui.py --once            跑一次只读动作后退出
"""
import os
import sys
import json
import queue
import shutil
import ctypes
import logging
import logging.handlers
import datetime
import threading
import subprocess
import traceback

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("AWB_ROOT", BASE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import awb_config as C
import awb_matrix as M

VERSION = "1.1.0"
APP_TITLE = "AWB 控制台 · 多 agent 共享工作空间"
OFFSCREEN = "--offscreen" in sys.argv   # 无打扰的窗口测试：把窗口放到屏幕外

COL_W = 122          # 每个工作空间列宽（像素）
NAME_W = 210         # 技能名列宽（像素）
HEAD_H = 46          # 列头高度
ROW_H = 27           # 行高
GLOBAL_COL = "__global__"

LOG_FILE = os.path.join(BASE, "awb_gui.log")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- 基础工具

def _find_python():
    """打包成 exe 后需要外部解释器跑 CLI 脚本；AWB_PYTHON 可显式指定。"""
    if not FROZEN:
        return sys.executable
    for cand in (os.environ.get("AWB_PYTHON"), shutil.which("python"), shutil.which("py")):
        if cand and os.path.isfile(cand):
            return cand
    return "python"


PY = _find_python()
PS = "powershell.exe"


def script(name):
    return os.path.join(REPO, name)


def shell_open(path, log=False):
    """用 ShellExecuteW 打开；.log 交给记事本，避免无关联时静默失败。"""
    try:
        if log:
            ctypes.windll.shell32.ShellExecuteW(None, "open", "notepad.exe", path, None, 1)
        else:
            ctypes.windll.shell32.ShellExecuteW(None, "open", path, None, None, 1)
    except Exception as e:
        messagebox.showwarning(APP_TITLE, "打不开文件：\n%s\n\n%s" % (path, e))


class Tooltip:
    """悬停提示：每个列头、参数标签、技能名都带说明。"""

    def __init__(self, widget, text):
        self.w = widget
        self.text = text
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _e):
        self.job = self.w.after(420, self._show)

    def _show(self):
        if self.tip:
            return
        try:
            x = self.w.winfo_rootx() + 12
            y = self.w.winfo_rooty() + self.w.winfo_height() + 4
            self.tip = tk.Toplevel(self.w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry("+%d+%d" % (x, y))
            tk.Label(self.tip, text=self.text, justify="left", anchor="w",
                     relief="solid", bd=1, bg="#ffffe0", fg="#202020",
                     font=("Microsoft YaHei UI", 9), wraplength=380,
                     padx=8, pady=6).pack()
        except tk.TclError:
            self.tip = None

    def _hide(self, _e=None):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class Runner:
    """后台跑命令，输出经队列回到 UI 线程，界面不冻结。"""

    def __init__(self, root, log_fn, status_fn):
        self.root = root
        self.log = log_fn
        self.status = status_fn
        self.q = queue.Queue()
        self.busy = False
        self.label = ""
        self.after_cb = None
        self.final = None
        self.pending = []

    def run_many(self, jobs, final=None):
        """jobs: [(label, argv, encoding), ...]，串行执行。"""
        if self.busy:
            messagebox.showinfo(APP_TITLE, "还有命令在执行，等它跑完再试。")
            return
        self.pending = list(jobs)
        self.final = final
        self._next()

    def run(self, label, argv, enc="utf-8", after=None):
        self.run_many([(label, argv, enc)], final=after)

    def _next(self):
        if not self.pending:
            cb, self.final = self.final, None
            if cb:
                cb()
            return
        label, argv, enc = self.pending.pop(0)
        self.label = label
        self.busy = True
        self.status("%s …" % label)
        self.log("$ " + " ".join(str(a) for a in argv))
        t = threading.Thread(target=self._worker, args=(argv, enc), daemon=True)
        t.start()
        self.root.after(60, self._poll)

    def _worker(self, argv, enc):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            p = subprocess.Popen(argv, cwd=REPO, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=env, text=True,
                                 encoding=enc, errors="replace",
                                 creationflags=CREATE_NO_WINDOW)
            for line in p.stdout:
                self.q.put(("line", line.rstrip("\r\n")))
            rc = p.wait()
            self.q.put(("done", rc))
        except Exception as e:
            self.q.put(("line", "ERROR: %s" % e))
            self.q.put(("line", traceback.format_exc()))
            self.q.put(("done", 1))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self.log(val)
                else:
                    self.busy = False
                    self.status("%s 完成（退出码 %d）" % (self.label, val))
                    if val != 0:
                        self.log("! 上一条命令退出码非 0，请看上面的输出")
                    self.root.after(10, self._next)
                    return
        except queue.Empty:
            pass
        if self.busy:
            self.root.after(60, self._poll)


# ---------------------------------------------------------------- 主窗口

class App:
    def __init__(self, root):
        self.root = root
        self.vars = {}
        self.visible_skills = []
        self.visible_cols = []
        self.matrix_data, self.source = M.load_matrix()
        self.all_skills = M.vault_names()
        self.all_ws = M.list_workspaces()

        self.ws_var = tk.StringVar()
        self.show_mode = tk.StringVar(value="enabled")
        self.ws_filter = tk.StringVar()
        self.skill_filter = tk.StringVar()
        self.why_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.cross_ws_var = tk.StringVar(value="(全部)")

        root.title("%s  v%s" % (APP_TITLE, VERSION))
        root.geometry("1180x760-4000-4000" if OFFSCREEN else "1180x760")
        root.minsize(940, 620)
        try:
            root.iconbitmap(default="")
        except tk.TclError:
            pass

        self._init_logging()
        self._build_ui()
        self.rebuild(preserve=False)
        self.refresh_status()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log("AWB 控制台 v%s 已启动" % VERSION)
        self.log("配置：%s" % C.CFG)
        self.log("矩阵：%s（当前来源：%s）" % (M.MATRIX, self.source))
        self.log("工作空间根：%s（%d 个）" % (M.WORKSPACE_ROOT, len(self.all_ws)))
        self.log("技能保管库：%s（%d 个）" % (M.VAULT, len(self.all_skills)))

    def _init_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")],
        )

    # ------------------------------------------------------------ UI 骨架

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text=APP_TITLE, font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        ttk.Label(top, text="v%s" % VERSION, foreground="#666").pack(side="left", padx=(8, 0))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self.tab_matrix = ttk.Frame(self.nb)
        self.tab_share = ttk.Frame(self.nb)
        self.tab_cross = ttk.Frame(self.nb)
        self.tab_rules = ttk.Frame(self.nb)
        self.tab_misc = ttk.Frame(self.nb)
        self.nb.add(self.tab_matrix, text=" 技能分配矩阵 ")
        self.nb.add(self.tab_share, text=" 共享工作空间 ")
        self.nb.add(self.tab_cross, text=" 跨 agent 投影 ")
        self.nb.add(self.tab_rules, text=" 规则同步 (16 通道) ")
        self.nb.add(self.tab_misc, text=" 维护 / 关于 ")

        self._build_matrix_tab()
        self._build_share_tab()
        self._build_cross_tab()
        self._build_rules_tab()
        self._build_misc_tab()
        self._build_footer()

    def _build_footer(self):
        bar = ttk.Frame(self.root, padding=(10, 6))
        bar.pack(fill="x")
        self.status_lbl = ttk.Label(bar, textvariable=self.status_var, anchor="w",
                                    width=110, font=("Microsoft YaHei UI", 9))
        self.status_lbl.pack(side="left")
        Tooltip(self.status_lbl, "状态行是唯一真相：显示当前操作、来源、勾选统计。长文本悬停看全值。")
        ttk.Button(bar, text="清空日志", command=self.clear_log, width=10).pack(side="right")
        ttk.Button(bar, text="打开日志", command=lambda: shell_open(LOG_FILE, log=True), width=10).pack(side="right", padx=4)

        logbox = ttk.LabelFrame(self.root, text=" 运行输出 ", padding=(6, 4))
        logbox.pack(fill="both", padx=10, pady=(0, 8))
        self.log_text = tk.Text(logbox, height=9, wrap="none", state="disabled",
                                font=("Consolas", 9), bg="#fbfbfb", fg="#222")
        sb = ttk.Scrollbar(logbox, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        self.runner = Runner(self.root, self.log, self.set_status)

    # ------------------------------------------------------------ Tab 1 矩阵

    def _build_matrix_tab(self):
        bar = ttk.Frame(self.tab_matrix, padding=(8, 8))
        bar.pack(fill="x")
        b_reload = ttk.Button(bar, text="⟳ 重新读取", command=self.reload, width=12)
        b_reload.pack(side="left")
        Tooltip(b_reload, "从磁盘重读 skills-matrix.json；未保存的勾选会丢弃。")
        b_save = ttk.Button(bar, text="✓ 保存并应用", command=self.save_and_apply, width=14)
        b_save.pack(side="left", padx=6)
        Tooltip(b_save, "把勾选写进 skills-matrix.json，然后执行 scope_skills.py --apply 让全局与各工作空间立即生效。")
        b_clear = ttk.Button(bar, text="全清", command=self.clear_all, width=8)
        b_clear.pack(side="left")
        Tooltip(b_clear, "清空所有勾选（含全局列），需再点「保存并应用」才落盘。")
        b_reset = ttk.Button(bar, text="从规则表重建", command=self.reset_to_rules, width=14)
        b_reset.pack(side="left", padx=6)
        Tooltip(b_reset, "丢弃矩阵文件，回到 skills-workspaces.json 的 rules/bundles 模式。矩阵文件会备份成 .bak。")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(bar, text="显示：").pack(side="left")
        mode = ttk.Combobox(bar, textvariable=self.show_mode, width=12, state="readonly",
                            values=("enabled", "all"))
        mode.pack(side="left")
        Tooltip(mode, "enabled = 只列有专属技能的工作空间；all = 列出全部工作空间（列会很多，可横向滚动）。")
        mode.bind("<<ComboboxSelected>>", lambda _e: self.rebuild())

        ttk.Label(bar, text="工作空间：").pack(side="left", padx=(12, 2))
        e1 = ttk.Entry(bar, textvariable=self.ws_filter, width=14)
        e1.pack(side="left")
        Tooltip(e1, "按工作空间名过滤列，留空显示全部。输入即时生效。")
        ttk.Label(bar, text="技能：").pack(side="left", padx=(12, 2))
        e2 = ttk.Entry(bar, textvariable=self.skill_filter, width=14)
        e2.pack(side="left")
        Tooltip(e2, "按技能名过滤行，留空显示全部。输入即时生效。")
        self.ws_filter.trace_add("write", lambda *_a: self.rebuild())
        self.skill_filter.trace_add("write", lambda *_a: self.rebuild())

        self.source_lbl = ttk.Label(bar, text="", foreground="#555",
                                    font=("Microsoft YaHei UI", 9))
        self.source_lbl.pack(side="right")
        Tooltip(self.source_lbl, "当前分配事实源：matrix = 界面勾选文件；rules = skills-workspaces.json 里的正则规则展开。")

        hint = ttk.Label(self.tab_matrix,
                         text="行 = 技能，列 = 全局 / 工作空间；点列头切换整列，点技能名切换整行。勾选后必须点「保存并应用」才会落盘生效。",
                         foreground="#666", padding=(10, 2))
        hint.pack(fill="x")

        area = ttk.Frame(self.tab_matrix, padding=(8, 4))
        area.pack(fill="both", expand=True)

        self.left_canvas = tk.Canvas(area, width=NAME_W, highlightthickness=0, bg="#f5f6f8")
        self.right_canvas = tk.Canvas(area, highlightthickness=0, bg="#ffffff")
        vs = ttk.Scrollbar(area, orient="vertical", command=self._yview_both)
        hs = ttk.Scrollbar(area, orient="horizontal", command=self.right_canvas.xview)
        self.left_canvas.configure(yscrollcommand=vs.set)
        self.right_canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)

        self.left_canvas.grid(row=0, column=0, sticky="ns")
        self.right_canvas.grid(row=0, column=1, sticky="nsew")
        vs.grid(row=0, column=2, sticky="ns")
        hs.grid(row=1, column=1, sticky="ew")
        area.rowconfigure(0, weight=1)
        area.columnconfigure(1, weight=1)

        self.left_inner = tk.Frame(self.left_canvas, bg="#f5f6f8")
        self.right_inner = tk.Frame(self.right_canvas, bg="#ffffff")
        self.left_canvas.create_window((0, 0), window=self.left_inner, anchor="nw")
        self.right_canvas.create_window((0, 0), window=self.right_inner, anchor="nw")
        for w in (self.left_canvas, self.right_canvas):
            self._bind_wheel(w)

    def _bind_wheel(self, w):
        w.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, e):
        step = -3 if e.delta > 0 else 3
        self._yview_both("scroll", step, "units")

    def _yview_both(self, *args):
        self.left_canvas.yview(*args)
        self.right_canvas.yview(*args)

    def collect_state(self):
        st = {"global": set(), "ws": {}}
        if not self.vars:
            return None
        for (skill, col), var in self.vars.items():
            if var.get():
                if col == GLOBAL_COL:
                    st["global"].add(skill)
                else:
                    st["ws"].setdefault(col, set()).add(skill)
        return st

    def rebuild(self, preserve=True):
        state = self.collect_state() if preserve else None
        for w in self.left_inner.winfo_children():
            w.destroy()
        for w in self.right_inner.winfo_children():
            w.destroy()
        self.vars = {}

        data = self.matrix_data
        skills = list(self.all_skills)
        sf = self.skill_filter.get().strip().lower()
        if sf:
            skills = [s for s in skills if sf in s.lower()]
        wf = self.ws_filter.get().strip().lower()
        mode = self.show_mode.get()
        cols = [GLOBAL_COL]
        for w in self.all_ws:
            if mode == "enabled" and not data.get("workspaces", {}).get(w):
                continue
            if wf and wf not in w.lower():
                continue
            cols.append(w)

        self.visible_skills = skills
        self.visible_cols = cols
        total_h = HEAD_H + len(skills) * ROW_H + 8
        total_w = len(cols) * COL_W + 8
        self.left_inner.configure(width=NAME_W, height=total_h)
        self.right_inner.configure(width=total_w, height=total_h)
        self.left_canvas.configure(scrollregion=(0, 0, NAME_W, total_h))
        self.right_canvas.configure(scrollregion=(0, 0, total_w, total_h))

        hleft = tk.Label(self.left_inner, text="技能（点名切换整行）", anchor="w",
                         bg="#e8ecf3", font=("Microsoft YaHei UI", 9, "bold"),
                         relief="flat", padx=6)
        hleft.place(x=0, y=0, width=NAME_W, height=HEAD_H)
        Tooltip(hleft, "竖列是保管库里的全部技能。点技能名可一次切换该技能在所有列上的勾选。")
        self._bind_wheel(hleft)

        for i, col in enumerate(cols):
            if col == GLOBAL_COL:
                title = "全局\n(所有工作空间)"
                tip = "全局列：勾上后技能出现在用户级 ~/.workbuddy/skills/，所有工作空间都能看到。与工作空间专属列互不冲突。"
            else:
                title = col if len(col) <= 15 else col[:14] + "…"
                tip = ("工作空间：%s\n勾上 = 该工作空间专属启用此技能，只出现在 <workspace>/.codebuddy/skills/ 等目录。\n"
                       "点击列头可一次切换整列。" % col)
            b = tk.Button(self.right_inner, text=title, relief="raised", bd=1,
                          bg="#dfe7f2" if col == GLOBAL_COL else "#eef1f6",
                          font=("Microsoft YaHei UI", 9, "bold"),
                          command=lambda c=col: self.toggle_col(c))
            b.place(x=i * COL_W, y=0, width=COL_W - 2, height=HEAD_H - 2)
            Tooltip(b, tip)
            self._bind_wheel(b)

        for r, skill in enumerate(skills):
            y = HEAD_H + r * ROW_H
            bg = "#ffffff" if r % 2 == 0 else "#f7f9fc"
            lbl = tk.Label(self.left_inner, text=skill, anchor="w", bg=bg,
                           font=("Consolas", 10), padx=8,
                           cursor="hand2")
            lbl.place(x=0, y=y, width=NAME_W, height=ROW_H - 1)
            lbl.bind("<Button-1>", lambda _e, s=skill: self.toggle_row(s))
            Tooltip(lbl, "技能 %s\n点击一次切换该技能在「全局 + 所有可见工作空间」上的勾选。" % skill)
            self._bind_wheel(lbl)

            for i, col in enumerate(cols):
                if state is not None:
                    on = (skill in state["global"]) if col == GLOBAL_COL else (skill in state["ws"].get(col, set()))
                elif col == GLOBAL_COL:
                    on = skill in data.get("global", [])
                else:
                    on = skill in data.get("workspaces", {}).get(col, [])
                var = tk.BooleanVar(value=bool(on))
                cb = tk.Checkbutton(self.right_inner, variable=var, bg=bg,
                                    activebackground=bg, bd=0, highlightthickness=0)
                cb.place(x=i * COL_W + (COL_W - 22) // 2, y=y + 2,
                         width=22, height=ROW_H - 5)
                Tooltip(cb, "%s × %s\n%s = 启用。取消勾选后记得点「保存并应用」。" %
                        (skill, ("全局" if col == GLOBAL_COL else col), ("☑" if on else "☐")))
                self._bind_wheel(cb)
                self.vars[(skill, col)] = var

        self.right_canvas.xview_moveto(0)
        self.left_canvas.yview_moveto(0)
        self.right_canvas.yview_moveto(0)
        self.refresh_status()

    def toggle_col(self, col):
        if not self.visible_skills:
            return
        all_on = all(self.vars[(s, col)].get() for s in self.visible_skills if (s, col) in self.vars)
        for s in self.visible_skills:
            if (s, col) in self.vars:
                self.vars[(s, col)].set(not all_on)
        self.refresh_status()

    def toggle_row(self, skill):
        cols = [c for c in self.visible_cols if (skill, c) in self.vars]
        if not cols:
            return
        all_on = all(self.vars[(skill, c)].get() for c in cols)
        for c in cols:
            self.vars[(skill, c)].set(not all_on)
        self.refresh_status()

    def clear_all(self):
        for v in self.vars.values():
            v.set(False)
        self.refresh_status()
        self.log("已清空全部勾选（尚未落盘）")

    def refresh_status(self):
        st = self.collect_state()
        if st is None:
            n = sum(1 for v in self.vars.values() if v.get())
        else:
            n = len(st["global"]) + sum(len(x) for x in st["ws"].values())
        self.set_status("就绪 | 来源: %s | 技能 %d 行 | 工作空间 %d 列 | 已勾选 %d" %
                        (self.source, len(self.visible_skills),
                         max(0, len(self.visible_cols) - 1), n))
        if hasattr(self, "source_lbl"):
            self.source_lbl.configure(text="事实源：%s" % ("矩阵文件（界面勾选）" if self.source == "matrix" else "规则表展开"))

    def reload(self):
        self.matrix_data, self.source = M.load_matrix()
        self.all_skills = M.vault_names()
        self.all_ws = M.list_workspaces()
        self.rebuild(preserve=False)
        self.log("已从磁盘重新读取；来源 = %s" % self.source)

    def save_and_apply(self):
        st = self.collect_state()
        if st is None:
            return
        data = {
            "note": M.NOTE,
            "source": "matrix",
            "global": [s for s in self.all_skills if s in st["global"]],
            "workspaces": {w: [s for s in self.all_skills if s in st["ws"].get(w, set())]
                           for w in self.all_ws},
        }
        try:
            path = M.save_matrix(data)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "写矩阵文件失败：\n%s" % e)
            return
        self.matrix_data, self.source = M.load_matrix()
        self.log("矩阵已保存：%s（全局 %d 项，%d 个工作空间）" %
                 (path, len(data["global"]), len(data["workspaces"])))
        self.runner.run("应用技能分配", [PY, script("scope_skills.py"), "--apply"],
                        after=self._after_apply)

    def _after_apply(self):
        self.refresh_status()
        shared = [w for w in self.all_ws
                  if os.path.isdir(os.path.join(M.WORKSPACE_ROOT, w, ".shared", "skills"))]
        if shared:
            self.log("检测到 %d 个已建共享层的工作空间：%s" % (len(shared), ", ".join(shared)))
            self.log("共享层的 canonical 已随 --apply 同步；如需重建链接结构，到「共享工作空间」页点「建立 / 刷新」。")
        self.log("提示：跨 agent 项目级目录如需同步，到「跨 agent 投影」页点「应用」。")

    def reset_to_rules(self):
        if not os.path.isfile(M.MATRIX):
            self.log("当前本来就是 rules 模式，无需处理。")
            return
        if not messagebox.askyesno(APP_TITLE,
                                   "丢弃 skills-matrix.json，回到规则表模式？\n\n"
                                   "矩阵文件会备份为 .bak-<时间戳>，界面勾选将失效。\n"
                                   "规则表本身不会被修改。"):
            return
        bak = M.MATRIX + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            os.rename(M.MATRIX, bak)
        except OSError as e:
            messagebox.showerror(APP_TITLE, "备份/删除失败：\n%s" % e)
            return
        self.log("已回退到规则表模式；矩阵备份：%s" % bak)
        self.reload()

    # ------------------------------------------------------------ Tab 2 共享层

    def _build_share_tab(self):
        bar = ttk.Frame(self.tab_share, padding=(10, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="工作空间：").pack(side="left")
        self.ws_combo = ttk.Combobox(bar, textvariable=self.ws_var, width=34, state="readonly",
                                     values=self._ws_values())
        self.ws_combo.pack(side="left", padx=4)
        if self.all_ws:
            self.ws_combo.set(self._default_ws())
        Tooltip(self.ws_combo, "选择要操作的逻辑工作空间。各 agent 的同一逻辑工作空间指向同一物理目录。")
        ttk.Button(bar, text="刷新列表", command=self._refresh_ws_combos, width=10).pack(side="left", padx=4)

        grid = ttk.Frame(self.tab_share, padding=(10, 4))
        grid.pack(fill="x")
        actions = [
            ("建立 / 刷新共享层", "创建 .shared/skills 与 .shared/memory，把 7 家 agent 项目级目录改成 Junction 指过去。已有真实文件会备份。", self.share_build),
            ("状态", "只读查看各目录是否已是 Junction、指向哪里、有多少实体。", self.share_status),
            ("落盘即见验证", "在 canonical 写临时探针，检查 8 个 agent 路径是否都能读到，然后删除探针。", self.share_probe),
            ("漂移检查", "校验链接指向、技能集合与矩阵是否一致。有问题时退出码 1，可挂 CI。", self.share_check),
            ("生成快照", "把当前共享层清单存成 snapshot-<时间戳>.json，供差异对照。", self.share_snapshot),
            ("恢复快照…", "选一份快照，报告技能集合差异并重建链接结构（不删实体文件）。", self.share_restore),
        ]
        for i, (text, tip, fn) in enumerate(actions):
            b = ttk.Button(grid, text=text, command=fn, width=20)
            b.grid(row=i // 3, column=i % 3, padx=6, pady=6, sticky="w")
            Tooltip(b, tip)

        wf = ttk.LabelFrame(self.tab_share, text=" 溯源 ", padding=(10, 8))
        wf.pack(fill="x", padx=10, pady=8)
        ttk.Label(wf, text="agent 路径：").pack(side="left")
        self.why_combo = ttk.Combobox(wf, textvariable=self.why_var, width=26,
                                      values=[".codebuddy/skills", ".workbuddy/skills", ".agents/skills",
                                              ".codex/skills", ".claude/skills", ".qoder/skills",
                                              ".trae/skills", ".workbuddy/memory"])
        self.why_combo.set(".codex/skills")
        self.why_combo.pack(side="left", padx=4)
        Tooltip(self.why_combo, "选一个客户端项目级目录，查看它实际指向哪个 canonical 目录。")
        b = ttk.Button(wf, text="查看来源", command=self.share_why, width=12)
        b.pack(side="left", padx=6)
        Tooltip(b, "执行 share_workspace.py --why <路径>，输出 JUNCTION 目标与 canonical 绝对路径。")

        info = ttk.Label(self.tab_share, justify="left", foreground="#555", padding=(12, 4),
                         text="共享层只放工作空间的知识、进度与技能集合；各 agent 的私有会话记忆不并入。\n"
                              "技能集合改在「技能分配矩阵」页，改完点「保存并应用」即可；此处按钮只管链接结构与校验。")
        info.pack(fill="x")

    def _ws_values(self):
        return ["(全部)"] + list(self.all_ws)

    def _default_ws(self):
        m, _ = M.load_matrix()
        for w in self.all_ws:
            if m.get("workspaces", {}).get(w):
                return w
        return self.all_ws[0] if self.all_ws else "(全部)"

    def _refresh_ws_combos(self):
        self.all_ws = M.list_workspaces()
        for combo in (getattr(self, "ws_combo", None), getattr(self, "cross_combo", None)):
            if combo is not None:
                try:
                    combo.configure(values=self._ws_values())
                except tk.TclError:
                    pass
        self.log("工作空间列表已刷新（%d 个）" % len(self.all_ws))

    def _ws_args(self, allow_all=False):
        w = self.ws_var.get().strip()
        if not w or w == "(全部)":
            return ["--all"] if allow_all else None
        return ["--workspace", w]

    def share_build(self):
        a = self._ws_args(allow_all=True)
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("建立共享层", [PY, script("share_workspace.py")] + a)

    def share_status(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("共享层状态", [PY, script("share_workspace.py"), "--status"] + a)

    def share_probe(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("落盘即见验证", [PY, script("share_workspace.py"), "--probe"] + a)

    def share_check(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("漂移检查", [PY, script("share_workspace.py"), "--check"] + a)

    def share_snapshot(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("生成快照", [PY, script("share_workspace.py"), "--snapshot"] + a)

    def share_restore(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        ws = self.ws_var.get().strip()
        start = os.path.join(M.WORKSPACE_ROOT, ws, ".shared")
        p = filedialog.askopenfilename(title="选择快照", initialdir=start,
                                       filetypes=[("快照 JSON", "snapshot-*.json"), ("JSON", "*.json")])
        if not p:
            return
        self.runner.run("恢复快照", [PY, script("share_workspace.py"), "--restore", p] + a)

    def share_why(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        rel = self.why_var.get().strip()
        if not rel:
            return
        self.runner.run("溯源 " + rel, [PY, script("share_workspace.py"), "--why", rel] + a)

    # ------------------------------------------------------------ Tab 3 跨 agent

    def _build_cross_tab(self):
        bar = ttk.Frame(self.tab_cross, padding=(10, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="工作空间：").pack(side="left")
        self.cross_combo = ttk.Combobox(bar, textvariable=self.cross_ws_var, width=34,
                                        state="readonly", values=self._ws_values())
        self.cross_combo.set("(全部)")
        self.cross_combo.pack(side="left", padx=4)
        Tooltip(self.cross_combo, "只处理某个工作空间，或 (全部) 处理所有有专属技能的工作空间。")
        b1 = ttk.Button(bar, text="预览（dry-run）", command=lambda: self.cross(False), width=18)
        b1.pack(side="left", padx=6)
        Tooltip(b1, "只打印将要创建的链接，不写任何文件。")
        b2 = ttk.Button(bar, text="应用到各 agent 项目级目录", command=lambda: self.cross(True), width=24)
        b2.pack(side="left", padx=4)
        Tooltip(b2, "把专属技能链接铺到 .agents/.codex/.claude/.qoder/.trae 等各家项目级目录；任何用户级技能池都不动。")

        ttk.Label(self.tab_cross, foreground="#666", padding=(12, 2),
                  text="注意：Claude / Qoder 是「用户级覆盖项目级」，与 WorkBuddy 相反；"
                       "若它们的用户级池已有同名技能，项目级追加只是同名优先，不会减少全局清单。").pack(fill="x")

        frame = ttk.LabelFrame(self.tab_cross, text=" 各客户端路径对照（configs/agent-skill-paths.json） ", padding=(8, 6))
        frame.pack(fill="both", expand=True, padx=10, pady=8)
        cols = ("name", "product", "project_dirs", "precedence", "isolation")
        self.path_tree = ttk.Treeview(frame, columns=cols, show="headings", height=9)
        heads = {"name": "标识", "product": "客户端", "project_dirs": "项目级 skills 目录",
                 "precedence": "同名优先级", "isolation": "工作空间隔离"}
        widths = {"name": 90, "product": 170, "project_dirs": 230, "precedence": 260, "isolation": 110}
        for c in cols:
            self.path_tree.heading(c, text=heads[c])
            self.path_tree.column(c, width=widths[c], anchor="w")
        Tooltip(self.path_tree, "来自 configs/agent-skill-paths.json 的静态对照表："
                "各客户端的项目级 skills 目录、同名优先级方向、是否支持工作空间隔离。")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.path_tree.yview)
        self.path_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.path_tree.pack(side="left", fill="both", expand=True)
        self._fill_path_tree()
        ttk.Button(self.tab_cross, text="打开路径映射表", command=lambda: shell_open(C.AGENT_PATHS),
                   width=18).pack(anchor="w", padx=12, pady=(0, 8))

    def _fill_path_tree(self):
        for row in self.path_tree.get_children():
            self.path_tree.delete(row)
        try:
            with open(C.AGENT_PATHS, encoding="utf-8") as f:
                x = json.load(f)
            for a in x.get("agents", []):
                self.path_tree.insert("", "end", values=(
                    a.get("name", ""), a.get("product", ""),
                    ", ".join(a.get("project_dirs", [])) or "(无)",
                    a.get("precedence", ""), str(a.get("workspace_isolation", ""))))
        except Exception as e:
            self.log("路径映射表读取失败：%s" % e)

    def cross(self, apply):
        argv = [PY, script("cross_agent_skills.py")]
        if apply:
            argv.append("--apply")
        w = self.cross_ws_var.get().strip()
        if w and w != "(全部)":
            argv += ["--workspace", w]
        if apply and not messagebox.askyesno(APP_TITLE,
                                             "确认把专属技能铺到各 agent 的项目级 skills 目录？\n\n"
                                             "只追加项目级链接，不会动任何用户级技能池。"):
            return
        self.runner.run("跨 agent 投影（应用）" if apply else "跨 agent 投影（预览）", argv)

    # ------------------------------------------------------------ Tab 4 规则同步

    def _build_rules_tab(self):
        bar = ttk.Frame(self.tab_rules, padding=(10, 10))
        bar.pack(fill="x")
        acts = [
            ("状态", "-Status", "打印一致性摘要：目标数、块数、检查点数。"),
            ("Lint", "-Lint", "检查块长度、密钥、重复行；有问题直接报出来。"),
            ("预演更新", "-DryRun -Update", "预览将要写入的 16 个规则文件，不落盘。"),
            ("更新（分发）", "-Update", "把 snippet 里的块分发到 16 个客户端规则文件，改动前自动 .bak 备份。"),
            ("检查", "-Check", "只读一致性检查，drift 时退出码 1。"),
        ]
        for text, flag, tip in acts:
            b = ttk.Button(bar, text=text, width=16,
                           command=lambda f=flag, t=text: self.rules(t, f))
            b.pack(side="left", padx=5)
            Tooltip(b, tip + "\n脚本：sync-agent-memory.ps1")

        f = ttk.LabelFrame(self.tab_rules, text=" 规则块源 ", padding=(10, 8))
        f.pack(fill="x", padx=10, pady=10)
        ttk.Label(f, text=C.SNIPPET, font=("Consolas", 9)).pack(anchor="w")
        b = ttk.Button(f, text="打开 snippet", command=lambda: shell_open(C.SNIPPET), width=16)
        b.pack(anchor="w", pady=(8, 0))
        Tooltip(b, "snippet 里的 <!-- block -->…<!-- /block --> 每块会同步到各客户端规则文件。")

        ttk.Label(self.tab_rules, justify="left", foreground="#555", padding=(12, 4),
                  text="改规则内容只改这一份 snippet，再点「更新（分发）」。\n"
                       "每次 -Update 会给被改动的文件留 .bak-<时间戳>，可随时比对回滚。\n"
                       "-Check 的退出码 1 可直接挂 CI 或 pre-commit。").pack(fill="x")

    def rules(self, label, flags):
        argv = [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                script("sync-agent-memory.ps1")] + flags.split()
        self.runner.run("规则同步 · " + label, argv, enc="mbcs")

    # ------------------------------------------------------------ Tab 5 维护

    def _build_misc_tab(self):
        g1 = ttk.LabelFrame(self.tab_misc, text=" 技能隔离 ", padding=(10, 8))
        g1.pack(fill="x", padx=10, pady=8)
        b = ttk.Button(g1, text="恢复用户级全量（取消隔离）", command=self.restore_all, width=28)
        b.pack(side="left", padx=4)
        Tooltip(b, "执行 skill_restore.py：把保管库里的全部技能链接放回用户级目录，所有工作空间重新看到全量。矩阵文件仍保留。")
        b2 = ttk.Button(g1, text="清理残留的其他 agent 目录", command=self.clean_agents, width=28)
        b2.pack(side="left", padx=4)
        Tooltip(b2, "执行 scope_skills.py --clean-agents：清掉当前工作空间里 .claude/.codex/.agents/.gemini 下多余的临时技能链接。")

        g2 = ttk.LabelFrame(self.tab_misc, text=" 打开配置与产物 ", padding=(10, 8))
        g2.pack(fill="x", padx=10, pady=8)
        for text, path, islog in [
            ("技能分配表", C.CFG, False),
            ("矩阵文件", M.MATRIX, False),
            ("路径映射表", C.AGENT_PATHS, False),
            ("规则块 snippet", C.SNIPPET, False),
            ("运行日志", LOG_FILE, True),
        ]:
            b = ttk.Button(g2, text=text, width=18,
                           command=lambda p=path, l=islog: shell_open(p, log=l))
            b.pack(side="left", padx=4, pady=4)
            Tooltip(b, "打开：%s" % path)

        g3 = ttk.LabelFrame(self.tab_misc, text=" 关于 ", padding=(10, 8))
        g3.pack(fill="both", expand=True, padx=10, pady=8)
        txt = (
            "AWB 控制台 v%s\n\n" % VERSION +
            "界面只编辑事实源、拼命令、显示输出；所有实际操作由同目录的 CLI 脚本完成，\n"
            "命令行与界面行为完全一致，可以随时脱离界面使用。\n\n"
            "仓库目录：%s\n" % REPO +
            "Python：%s\n" % PY +
            "配置优先级：AWB_CONFIG 环境变量 > 同目录 config.json > 内置默认\n\n"
            "五个页面：\n"
            "  技能分配矩阵 —— 行=技能，列=全局/工作空间，打勾后「保存并应用」\n"
            "  共享工作空间 —— canonical + Junction，落盘即见\n"
            "  跨 agent 投影 —— 把专属技能铺到各家项目级目录\n"
            "  规则同步 —— snippet 一次分发到 16 个客户端规则文件\n"
            "  维护 / 关于 —— 取消隔离、清理、打开配置\n\n"
            "自检：python awb_gui.py --selftest     单次只读：python awb_gui.py --once\n"
        )
        t = tk.Text(g3, height=12, wrap="word", font=("Microsoft YaHei UI", 9),
                    bg="#fafafa", relief="flat")
        t.insert("1.0", txt)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)
        ttk.Button(g3, text="再跑一次自检", command=self.selftest_button, width=16).pack(anchor="w", pady=(8, 0))

    def restore_all(self):
        if not messagebox.askyesno(APP_TITLE,
                                   "把保管库全部技能放回用户级目录？\n\n"
                                   "这会取消工作空间隔离，所有工作空间重新看到全量技能。\n"
                                   "矩阵文件本身保留，随时可以再「保存并应用」恢复隔离。"):
            return
        self.runner.run("恢复用户级全量", [PY, script("skill_restore.py")])

    def clean_agents(self):
        self.runner.run("清理残留 agent 目录", [PY, script("scope_skills.py"), "--clean-agents"])

    def selftest_button(self):
        ok, lines = run_selftest()
        for l in lines:
            self.log(l)
        self.set_status("自检 %s" % ("通过" if ok else "失败"))

    # ------------------------------------------------------------ 日志与状态

    def log(self, text):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[%s] %s\n" % (ts, text)
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass
        try:
            logging.info(text)
        except Exception:
            pass

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def set_status(self, text):
        self.status_var.set(text)
        try:
            self.status_lbl.update_idletasks()
        except tk.TclError:
            pass

    def on_close(self):
        if self.runner.busy:
            if not messagebox.askyesno(APP_TITLE, "还有命令在执行，确定退出？"):
                return
        self.log("退出")
        self.root.destroy()


# ---------------------------------------------------------------- 自检 / 单次

def run_selftest():
    lines, ok = [], True

    def chk(label, cond, detail=""):
        nonlocal ok
        lines.append("%s %s%s" % ("OK  " if cond else "FAIL", label, (" — " + detail) if detail else ""))
        if not cond:
            ok = False

    chk("Python 版本 >= 3.8", sys.version_info >= (3, 8), sys.version.split()[0])
    chk("tkinter 可用", "tkinter" in sys.modules)
    chk("Python 解释器存在", os.path.isfile(PY), PY)
    chk("分配表存在", os.path.isfile(C.CFG), C.CFG)
    chk("工作空间根存在", os.path.isdir(M.WORKSPACE_ROOT), M.WORKSPACE_ROOT)
    chk("技能保管库存在", os.path.isdir(M.VAULT), "%d 个技能" % len(M.vault_names()))
    chk("规则块 snippet 存在", os.path.isfile(C.SNIPPET), C.SNIPPET)
    chk("路径映射表存在", os.path.isfile(C.AGENT_PATHS), C.AGENT_PATHS)
    chk("矩阵目录可写", os.access(os.path.dirname(M.MATRIX), os.W_OK), os.path.dirname(M.MATRIX))
    try:
        m, src = M.load_matrix()
        chk("矩阵加载", isinstance(m.get("global"), list) and isinstance(m.get("workspaces"), dict),
            "来源=%s，%d 个工作空间" % (src, len(m["workspaces"])))
    except Exception as e:
        chk("矩阵加载", False, str(e))
    for f in ("scope_skills.py", "share_workspace.py", "cross_agent_skills.py",
              "skill_restore.py", "sync-agent-memory.ps1", "awb_matrix.py"):
        chk("脚本存在 " + f, os.path.isfile(script(f)))
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as h:
            h.write("")
        chk("日志文件可写", True, LOG_FILE)
    except Exception as e:
        chk("日志文件可写", False, str(e))
    try:
        r = subprocess.run([PY, "-c", "print('awb-ok')"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=20, creationflags=CREATE_NO_WINDOW)
        chk("子进程可执行", r.returncode == 0 and "awb-ok" in r.stdout)
    except Exception as e:
        chk("子进程可执行", False, str(e))
    try:
        if os.name == "nt":
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            k.CreateMutexW.restype = ctypes.c_void_p
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k.CreateMutexW(None, False, "Global\\AWB_GUI_SELFTEST")
            chk("单实例互斥量可创建", bool(h))
            if h:
                k.CloseHandle(h)
        else:
            chk("单实例互斥量（非 Windows 跳过）", True)
    except Exception as e:
        chk("单实例互斥量可创建", False, str(e))
    lines.append("SELFTEST %s" % ("OK" if ok else "FAILED"))
    return ok, lines


def run_once():
    cmds = [
        ("矩阵摘要", [PY, script("awb_matrix.py")], "utf-8"),
        ("技能分配状态", [PY, script("scope_skills.py"), "--status"], "utf-8"),
        ("规则一致性", [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        script("sync-agent-memory.ps1"), "-Check"], "mbcs"),
    ]
    rc = 0
    for label, argv, enc in cmds:
        print("=== %s ===" % label)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        p = subprocess.run(argv, cwd=REPO, capture_output=True, text=True,
                           encoding=enc, errors="replace", env=env,
                           creationflags=CREATE_NO_WINDOW)
        print((p.stdout or "").strip())
        if p.stderr:
            print((p.stderr or "").strip())
        rc += p.returncode
    print("ONCE DONE rc=%d" % rc)
    return rc


def _crash_log(text):
    """noconsole 打包后没有 stdout，异常落盘便于排查。"""
    try:
        with open(os.path.join(BASE, "awb_gui_error.log"), "a", encoding="utf-8") as f:
            f.write("[%s]\n%s\n" % (datetime.datetime.now().isoformat(), text))
    except Exception:
        pass


def _acquire_mutex():
    if os.name != "nt":
        return None
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)          # 成功时 Win32 不清 last error，必须先归零
    h = k.CreateMutexW(None, False, "Global\\AWB_GUI_SINGLETON")
    if not h or ctypes.get_last_error() == 183:
        return None
    return h


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        ok, lines = run_selftest()
        for l in lines:
            print(l)
        return 0 if ok else 1
    if "--once" in args:
        return run_once()

    mutex = _acquire_mutex()
    if mutex is None and os.name == "nt":
        _crash_log("mutex: another instance is already running")
        return 0

    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        _crash_log(traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
