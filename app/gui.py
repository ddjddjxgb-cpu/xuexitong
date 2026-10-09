"""gui — 图形界面（个人自用改造）。

Tkinter（Python 标准库，无额外依赖）。

线程模型
--------
  主线程    = Tkinter 事件循环，只做 UI 刷新（每 150ms 从队列取日志与状态）
  工作线程  = 调度循环：时段闸门 → run_scheduler → 间隔，与 CLI 的 loop 同一套逻辑

设计原则：**不复制内核**
--------
调度决策、任务账本、播放引擎、时段判定全部复用现有实现（scheduler 与 app.loop），
本模块只负责「界面 + 循环控制 + 日志搬运」。这样 GUI 与 CLI 的行为天然一致，
将来修内核也只需修一处。

与 CLI 的差异
--------
  - 不做交互式启动向导（GUI 本身就是引导），课程取自 config/courses.json 或
    state 里已有的活跃课程；都没有时在日志区提示先去填配置。
  - 停止是「优雅停止」：当前这一轮跑完才退出（与 Ctrl+C 一次同语义），
    避免强杀留下孤儿 Chrome 进程。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

from app.loop import (
    LOCK_PATH,
    _acquire_lock,
    _active_course_desc,
    _ensure_active_course,
    _ensure_credentials,
    _in_window,
    _now_min,
    _parse_window,
    _seconds_until_open,
)
from utils.paths import repo_root

MAX_LOG_CHARS = 200_000      # 日志区字符上限，超出后裁掉前半，避免长跑吃内存
UI_TICK_MS = 150


class _QueueWriter:
    """把 print() 输出转发到 UI 队列（临时替换 sys.stdout）。

    只转发到队列、不在工作线程里碰 Tkinter —— Tk 不是线程安全的，
    所有 UI 操作必须留在主线程。
    """

    def __init__(self, q: "queue.Queue[tuple]") -> None:
        self._q = q

    def write(self, s: str) -> None:
        if s:
            self._q.put(("log", s))

    def flush(self) -> None:      # print() 会调
        pass


class XuexitongGUI:
    """简洁界面：一屏看全状态 + 两个按钮 + 一块日志。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: "threading.Thread | None" = None
        self.lock_fh = None
        self._last_play_refresh = 0.0        # 播放进度节流刷新时间戳
        self._build_ui()
        self._refresh_chapters()              # 首次填充章节列表
        self.root.after(UI_TICK_MS, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 界面构建 ────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.root.title("学习通自动播放")
        self.root.geometry("780x740")
        self.root.minsize(660, 560)

        # 课程信息
        top = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="课程", width=6).pack(side="left")
        self.var_course = tk.StringVar(value=_active_course_desc())
        ttk.Label(top, textvariable=self.var_course,
                  foreground="#1a4d8f").pack(side="left", fill="x", expand=True)

        # 状态区
        mid = ttk.Frame(self.root, padding=(12, 2, 12, 4))
        mid.pack(fill="x")
        self.var_status = tk.StringVar(value="未启动")
        self.var_cycle = tk.StringVar(value="轮次 -")
        self.var_play = tk.StringVar(value="")
        self.var_prog = tk.StringVar(value="进度 -")
        ttk.Label(mid, text="状态", width=6).grid(row=0, column=0, sticky="w")
        ttk.Label(mid, textvariable=self.var_status,
                  foreground="#0a7d33").grid(row=0, column=1, sticky="w")
        ttk.Label(mid, textvariable=self.var_cycle).grid(row=1, column=1, sticky="w")
        ttk.Label(mid, textvariable=self.var_play,
                  foreground="#b8860b").grid(row=2, column=1, sticky="w")
        ttk.Label(mid, textvariable=self.var_prog).grid(row=3, column=1, sticky="w")

        # 配置摘要（用 StringVar 以便重载 .env 后就地刷新）
        cfg = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        cfg.pack(fill="x")
        ttk.Label(cfg, text="配置", width=6).pack(side="left")
        self.var_cfg = tk.StringVar(value=self._config_summary())
        ttk.Label(cfg, textvariable=self.var_cfg,
                  foreground="#555555").pack(side="left")

        # 按钮
        bar = ttk.Frame(self.root, padding=(12, 4, 12, 8))
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="开始", command=self._on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self._on_stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="刷新进度",
                   command=self._refresh_chapters).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开数据目录",
                   command=self._on_open_dir).pack(side="right")

        # 章节进度（回答「哪些章完成了、哪些还没」）
        cf = ttk.LabelFrame(self.root, text=" 章节进度 ", padding=(6, 4))
        cf.pack(fill="x", padx=12, pady=(0, 6))
        cols = ("title", "state", "video", "manual")
        self.tree = ttk.Treeview(cf, columns=cols, show="headings", height=8)
        self.tree.heading("title", text="章节")
        self.tree.heading("state", text="状态")
        self.tree.heading("video", text="视频进度")
        self.tree.heading("manual", text="需你手动")
        self.tree.column("title", width=360, anchor="w")
        self.tree.column("state", width=110, anchor="center")
        self.tree.column("video", width=90, anchor="center")
        self.tree.column("manual", width=90, anchor="center")
        tsb = ttk.Scrollbar(cf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        # 日志
        wrap = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(wrap, height=18, wrap="none", state="disabled",
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4",
                           font=("Consolas", 9))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        self._log_line(f"工作目录：{repo_root()}")
        self._log_line("点「开始」启动。停止为优雅停止（当前这轮跑完才退出）。")

    @staticmethod
    def _chapter_status_rows() -> "list[tuple[str, str, str, str]]":
        """从账本与快照生成 (章节标题, 状态, 视频进度, 需手动) 行。

        完成态**以账本为唯一判据**（不另造计数）；视频进度取自点级快照。
        任何读取失败都返回空列表 —— 界面不能因数据缺失而崩。

        「需手动」统计的是**非视频且未完成**的任务点数（超星的 PDF / 做题 /
        测验等）。本工具只负责视频播放，所以这一列不为 0 的章节即使视频刷完
        也不会在学习通显示为「已完成」—— 需要用户自己把那些点做掉。
        """
        import json
        from models import resolve_account_id
        from state.course_state import load_active_course

        active = load_active_course()
        if not active:
            return []
        base = (repo_root() / "state" / "accounts" / str(resolve_account_id())
                / "registry" / active.key())
        tasks: dict = {}
        snap: dict = {}
        for name, box in (("tasks.json", tasks), ("chapter_points.json", snap)):
            try:
                box.update(json.loads((base / name).read_text(encoding="utf-8")))
            except Exception:
                pass

        per: dict = {}
        for tid, t in tasks.items():
            cid = str(t.get("chapter_id") or str(tid).split(":")[0])
            row = per.setdefault(cid, {"title": t.get("title") or cid,
                                       "total": 0, "done": 0, "bad": 0,
                                       "manual": 0})
            st = t.get("status")
            is_video = (t.get("task_type") or "video") == "video"
            if not is_video and st != "COMPLETED":
                row["manual"] += 1        # PDF / 做题 / 测验 —— 工具做不了
            if is_video:
                row["total"] += 1
                if st == "COMPLETED":
                    row["done"] += 1
                elif st in ("FAILED", "BLOCKED"):
                    row["bad"] += 1

        out: list[tuple[str, str, str, str]] = []
        for cid, r in sorted(per.items()):
            s = snap.get(cid) or {}
            vt, vf = s.get("video_total"), s.get("video_finished")
            video = f"{vf}/{vt}" if isinstance(vt, int) and vt else f"{r['done']}/{r['total']}"
            if r["bad"]:
                state = f"⚠ {r['bad']} 项失败"
            elif r["total"] and r["done"] == r["total"]:
                state = "✓ 视频已刷完"
            elif r["done"]:
                state = f"播放中 {r['done']}/{r['total']}"
            else:
                state = "待播放"
            manual = str(r["manual"]) if r["manual"] else "-"
            out.append((r["title"], state, video, manual))
        return out

    def _refresh_chapters(self) -> None:
        """把章节状态渲染进列表（**必须在主线程调用**）。"""
        try:
            rows = self._chapter_status_rows()
        except Exception as e:
            rows = []
            self._log_line(f"[gui] 章节列表刷新失败：{type(e).__name__}: {e}\n")
        kids = self.tree.get_children()
        if kids:
            self.tree.delete(*kids)
        for title, state, video, manual in rows:
            self.tree.insert("", "end", values=(title, state, video, manual))

    def _read_playback_progress(self) -> str:
        """从**最新的子进程日志**里提取播放进度 —— GUI 唯一的播放可见性来源。

        为什么必须读文件而不靠 queue：调度器把子进程的 stdout 直接重定向到
        `evidence/chapter_<id>.scheduler.stdout.log`（scheduler.py 的 stdout_fh），
        播放进度行（`[GHA] ct=…`）只进那个文件，不经过 GUI 的消息队列。
        不读它 → 程序在播而界面毫无反应，看起来就像「播放没实现」。

        只读文件尾部（子进程在持续追加），任何异常都降级为空串。
        """
        import glob
        import os
        import re

        try:
            files = glob.glob(str(repo_root() / "evidence"
                                     / "chapter_*.scheduler.stdout.log"))
            if not files:
                return ""
            latest = max(files, key=os.path.getmtime)
            with open(latest, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-6000:]
        except Exception:
            return ""

        hits = re.findall(r"\[GHA\] ct=(\d+)/(\S+) \((\d+)%\)", tail)
        if hits:
            ct, dur, pct = hits[-1]
            rate = ""
            mr = re.findall(r"playbackRate=([\d.]+)x", tail)
            if mr:
                rate = f" ｜ {mr[-1]}x"
            return f"▶ 播放中 {pct}%  ({ct}/{dur}s){rate}"
        if "★ isPassed=true" in tail:
            return "✓ 已完成（服务端已确认）"
        if "TargetClosedError" in tail or "crashed" in tail:
            return "✗ 浏览器被关闭（勿手动关窗口）"
        if "Video ready" in tail:
            return "… 已定位视频，等待起播"
        return ""

    def _refresh_play(self) -> None:
        """把播放进度刷到界面（主线程调用）。"""
        self.var_play.set(self._read_playback_progress())

    @staticmethod
    def _config_summary() -> str:
        """读当前生效的倍速与时段（只读展示，不含凭据）。"""
        import os
        rate = (os.environ.get("XUE_PLAYBACK_RATE") or "2").strip()
        win = (os.environ.get("XUE_LOOP_WINDOW") or "").strip()
        br = (os.environ.get("XUE_BROWSER_CHANNEL") or "").strip() or "内置"
        return f"倍速 {rate}x ｜ 时段 {win or '全天'} ｜ 浏览器 {br}"

    # ── 主线程：UI 刷新 ─────────────────────────────────────────────
    def _pump(self) -> None:
        """每 150ms 把队列里的日志/状态刷到界面。"""
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "cycle":
                    self.var_cycle.set(payload)
                elif kind == "prog":
                    self.var_prog.set(payload)
                elif kind == "course":
                    self.var_course.set(payload)
                elif kind == "config":
                    self.var_cfg.set(payload)
                elif kind == "refresh":
                    self._refresh_chapters()      # 主线程刷新章节列表
                elif kind == "done":
                    self._worker_done(payload)
        except queue.Empty:
            pass
        # 播放进度来自子进程日志文件（不经本队列），故独立节流刷新
        if time.time() - self._last_play_refresh >= 1.0:
            self._last_play_refresh = time.time()
            try:
                self._refresh_play()
            except Exception:
                pass
        self.root.after(UI_TICK_MS, self._pump)

    def _append(self, text: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", text)
        # 控制内存：超限就砍掉前面一半
        if self.txt.index("end-1c").split(".")[0] and \
                int(self.txt.index("end-1c").split(".")[0]) > 4000:
            self.txt.delete("1.0", "2000.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _log_line(self, text: str) -> None:
        self._append(text.rstrip() + "\n")

    # ── 按钮 ────────────────────────────────────────────────────────
    def _on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        # 单实例锁：与 CLI 共用同一个 lock 文件，防止 GUI 与 CLI 同时跑同一个账号
        self.lock_fh = _acquire_lock(LOCK_PATH)
        if self.lock_fh is None:
            self._log_line("⚠ 已有另一个实例在运行（GUI 或命令行）。"
                           "同一账号不要同时跑两份，会被学习通踢下线。")
            return
        self.stop_event.clear()
        self.var_status.set("启动中…")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def _on_stop(self) -> None:
        """优雅停止：置位后当前轮跑完退出（不打断正在进行的播放）。"""
        self.stop_event.set()
        self.var_status.set("停止中（等当前轮跑完）…")
        self.btn_stop.configure(state="disabled")

    def _on_open_dir(self) -> None:
        import os
        try:
            os.startfile(str(repo_root()))          # Windows
        except Exception:
            self._log_line(f"请手动打开：{repo_root()}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._log_line("正在停止工作线程…")
            self.root.after(300, self._try_close)
            return
        self._release_lock()
        self.root.destroy()

    def _try_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(300, self._try_close)
            return
        self._release_lock()
        self.root.destroy()

    def _release_lock(self) -> None:
        try:
            if self.lock_fh:
                self.lock_fh.close()
                self.lock_fh = None
        except Exception:
            pass

    # ── 工作线程：调度循环 ──────────────────────────────────────────
    def _run_worker(self) -> None:
        old_stdout = sys.stdout
        sys.stdout = _QueueWriter(self.q)
        try:
            self._worker_body()
        except Exception as e:
            self.q.put(("log", f"\n[gui] 工作线程异常：{type(e).__name__}: {e}\n"))
        finally:
            sys.stdout = old_stdout
            self.q.put(("done", None))

    def _worker_body(self) -> None:
        # 每次点「开始」都重新读一遍 .env —— 用户改了配置（时段/倍速/静音）后
        # 只需「停止」再「开始」即可生效，不必关窗口重开。
        # （override=True：.env 覆盖机器上残留的环境变量，这是 loop/exe 形态的语义）
        try:
            from utils.env_file import load_env_file
            reloaded = load_env_file(repo_root(), override=True)
            if reloaded:
                self.q.put(("log", "[gui] 已重载 .env："
                                   + "、".join(sorted(reloaded)) + "\n"))
        except Exception as e:
            self.q.put(("log", f"[gui] 重载 .env 失败（沿用当前环境）：{e}\n"))
        self.q.put(("config", self._config_summary()))    # 刷新界面上的配置摘要

        # 凭据：优先环境变量，其次项目根的 .env（与 CLI loop 同一实现）
        if not _ensure_credentials():
            self.q.put(("status", "缺少账号密码"))
            self.q.put(("log", "✗ 缺少学习通账号密码。请在项目根目录的 .env 里填写 "
                               "CX_USER / CX_PASS 后重开本程序。\n"))
            return
        # 课程：config/courses.json 优先，其次 state 里已有的活跃课程
        if not _ensure_active_course():
            self.q.put(("status", "未配置课程"))
            self.q.put(("log", "✗ 未配置课程。请把学习通『章节学习页』的完整 URL 填到 "
                               "config/courses.json 里。\n"))
            return

        self.q.put(("course", _active_course_desc()))
        self.q.put(("status", "运行中"))

        import os
        from scheduler import run_scheduler
        interval_min = 30
        active_min = 2
        try:
            interval_min = max(1, int(os.environ.get("XUE_LOOP_INTERVAL", "30")))
        except ValueError:
            pass
        try:
            active_min = max(1, int(os.environ.get("XUE_LOOP_ACTIVE_INTERVAL", "2")))
        except ValueError:
            pass
        window_spec = (os.environ.get("XUE_LOOP_WINDOW") or "").strip()
        win = _parse_window(window_spec)
        if window_spec and not win:
            self.q.put(("log", f"⚠ XUE_LOOP_WINDOW 格式无法解析：{window_spec!r}"
                               f"（应形如 23:00-07:00），本次按全天处理。\n"))

        error_streak = 0
        cycles = 0
        loop_start = time.time()

        while not self.stop_event.is_set():
            # 时段闸门（与 CLI 同一对纯函数）
            if not _in_window(_now_min(), win):
                wait_s = _seconds_until_open(_now_min(), win)
                hh, mm = divmod(wait_s // 60, 60)
                self.q.put(("status", f"等待时段（{window_spec}）"))
                self.q.put(("log", f"[gui] 当前不在运行时段（{window_spec}），"
                                   f"等待 {hh} 小时 {mm} 分钟后继续…\n"))
                self._sleep(wait_s, label="时段开启")
                continue

            cycles += 1
            self.q.put(("cycle", f"轮次 {cycles}"))
            self.q.put(("status", f"运行中（第 {cycles} 轮）"))
            self.q.put(("log", f"\n[gui] ── 第 {cycles} 轮 ── "
                               f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"))
            t0 = time.time()
            try:
                result = run_scheduler(None, "", "schedule",
                                       run_id=f"gui-{int(t0)}", max_chapters=1)
            except Exception as e:
                error_streak += 1
                self.q.put(("log", f"[gui] 本轮异常（{error_streak}/5）："
                                   f"{type(e).__name__}: {e}\n"))
                if error_streak >= 5:
                    self.q.put(("status", "连续异常已停止"))
                    self.q.put(("log", "[gui] 连续异常达上限，已停止。"
                                       "请检查网络/凭据/课程 URL。\n"))
                    return
                self._sleep(60)
                continue

            decision = result.decision
            verdict = result.verdict or ""
            self.q.put(("log", f"[gui] 本轮结果：decision={decision} "
                               f"result={result.result} verdict={verdict or '-'} "
                               f"({round(time.time() - t0, 1)}s)\n"))
            self._push_progress(result, loop_start)
            self.q.put(("refresh", None))       # 本轮结束 → 刷新章节列表

            if decision == "NOOP" and "No pending task" in verdict:
                self.q.put(("status", "已全部完成 ✅"))
                self.q.put(("log", "[gui] 课程已无可推进任务（完成或无可识别的视频点）。"
                                   "已停止。\n"))
                return
            if decision == "ERROR":
                error_streak += 1
                if error_streak >= 5:
                    self.q.put(("status", "连续 ERROR 已停止"))
                    self.q.put(("log", f"[gui] 连续 {error_streak} 轮 ERROR，已停止。"
                                       f"最后错误：{result.error or verdict}\n"))
                    return
            else:
                error_streak = 0

            if not self.stop_event.is_set():
                self._sleep((active_min if decision == "RUN" else interval_min) * 60)

        self.q.put(("status", "已停止"))

    def _sleep(self, total_s: float, label: str = "下一轮") -> None:
        """可被停止按钮打断的分段 sleep，并倒计时提示。

        label 区分为什么在等 —— 「时段开启」与「下一轮」对用户是完全不同的两件事，
        早先统一显示「等待下一轮」，会把「等到 23 点才开始」误读成「正在正常轮转」。
        """
        deadline = time.time() + total_s
        while not self.stop_event.is_set():
            remain = deadline - time.time()
            if remain <= 0:
                return
            if remain > 120:
                self.q.put(("status", f"等待{label}（{int(remain // 60)} 分钟后）"))
            self.stop_event.wait(min(30.0, remain))

    def _push_progress(self, result, loop_start: float) -> None:
        """把进度推到界面（与 CLI 的 _print_progress 同一数据源）。"""
        from state.course_state import load_active_course, load_course_state
        done = total = None
        active = load_active_course()
        if active:
            prog = getattr(load_course_state(active.key()), "progress", None)
            done = getattr(prog, "completed", None)
            total = getattr(prog, "total", None)
        prog_s = (f"{done}/{total}"
                  if isinstance(done, int) and isinstance(total, int) else "?")
        attempted = getattr(result, "chapters_attempted", None) or []
        chap = str(attempted[-1]) if attempted else "-"
        elapsed = int(max(0.0, time.time() - loop_start))
        hh, rem = divmod(elapsed, 3600)
        mm, ss = divmod(rem, 60)
        self.q.put(("prog", f"已完成 {prog_s} 章 ｜ 本轮章 {chap} ｜ "
                            f"已用 {hh:02d}:{mm:02d}:{ss:02d}"))
        self.q.put(("log", f"[进度] 已完成 {prog_s} 章 | 本轮章 {chap} | "
                           f"本次已用 {hh:02d}:{mm:02d}:{ss:02d}\n"))

    def _worker_done(self, _payload) -> None:
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._release_lock()


def main() -> int:
    """GUI 入口。返回进程退出码（Tkinter 主循环结束后）。"""
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")       # Windows 下更接近原生外观；失败则用默认
    except Exception:
        pass
    XuexitongGUI(root)
    root.mainloop()
    return 0
