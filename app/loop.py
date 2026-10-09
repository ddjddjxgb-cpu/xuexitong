"""loop — 本地常驻模式（exe 双击默认）：每轮调度一次，间隔可配。

GHA 模式靠 GitHub cron 周期性冷启动 `--action scheduler`；本地没有 cron，
所以 exe 无参数启动时进入本模块的常驻循环：

    循环： run_scheduler(trigger="schedule")   ← 与 GHA cron 同一决策引擎
           ↓ RUN / NOOP / BLOCKED + cooldown 熔断复位 全部继承
    sleep 轮间隔（默认 30 分钟，--interval-minutes / XUE_LOOP_INTERVAL 可配）

退出条件：
  - 课程完成（"No pending task"）→ 打印后正常退出（exit 0）
  - 连续 ERROR ≥ 5 → 打印后退出（exit 1，避免 URL 失效类错误静默死循环）
  - Ctrl+C → 优雅退出（第一次设停止位：当前轮跑完不再开下一轮；
    第二次立即退出）。状态每轮由 scheduler 落盘，无丢失窗口。

边界：
  - 不改 GHA 行为：仅 `--action loop` 走这里，initialize/run/scheduler 语义不变
  - 凭据：优先真实环境变量；缺失时读可写根 .env（utils.env_file，CI 行为不受
    影响——GHA 从不进 loop），仍缺失则交互输入并写回 .env（下次免输入）
  - 单实例：可写根 state/loop.lock 文件锁，防双开互踢会话
    （README「已知注意事项 4」：同账号并发会互相踢登录）
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

from utils.paths import repo_root
from utils.runlog import env_opt_out, install_run_log

LOCK_PATH = repo_root() / "state" / "loop.lock"
LOG_PATH = repo_root() / "evidence" / "loop.log"
ERROR_STREAK_EXIT = 5          # 连续 ERROR 次数上限，超过即退出
NO_ACTIVE_COURSE_MARK = "No active course"
NO_PENDING_MARK = "No pending task"
DEFAULT_INTERVAL_MIN = 30
DEFAULT_ACTIVE_INTERVAL_MIN = 2   # 活跃轮（刚推进过任务）后的短间隔，连续刷课


# ── 单实例锁 ────────────────────────────────────────────────────────

def _acquire_lock(lock_path: Path):
    """独占文件锁；拿到返回句柄（保持引用即持锁），拿不到返回 None。

    用 OS 级锁（Windows msvcrt / POSIX flock）而不是 O_EXCL 哨兵文件：
    进程死亡锁自动释放，不会留"僵尸锁"导致下次起不来。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


# ── 凭据 / 活跃课程引导 ─────────────────────────────────────────────

def _merge_env_file(env_path: Path, updates: dict) -> None:
    """合并写 .env：已有键**原地更新**，缺失键追加到末尾；注释与其他行保留。

    旧实现是无脑追加 —— 随包模板自带 `CX_USER=` 空键时，每次启动都会在文件
    尾部"另起写入"一对重复键（用户报障的原话），且先到先得的解析让追加的
    真值永远读不到。原子写回，避免中途崩溃留下半截文件。
    """
    lines = (env_path.read_text(encoding="utf-8").splitlines()
             if env_path.exists() else [])
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = None
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            seps = [i for i in (stripped.find("="), stripped.find(":")) if i > 0]
            if seps:
                k = stripped[:min(seps)].strip()
                if k in remaining:
                    key = k
        out.append(f"{key}={remaining.pop(key)}" if key else line)
    for k, v in remaining.items():
        out.append(f"{k}={v}")
    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(env_path)


def _ensure_credentials() -> bool:
    """保证 os.environ 里有 CX_USER/CX_PASS；缺失时引导输入并写 .env。

    优先级刻意与 CI 相反：**exe 旁 .env 覆盖机器环境变量**（override=True）。
    .env 是双击用户唯一的显式配置；用户机器上残留的 CX_USER（可能是别的账号）
    曾静默顶掉 .env，浏览器里填出的手机号和用户填的完全对不上。
    """
    from utils.env_file import load_env_file
    if not (os.environ.get("CX_USER") and os.environ.get("CX_PASS")):
        load_env_file(repo_root(), override=True)
    if os.environ.get("CX_USER") and os.environ.get("CX_PASS"):
        return True

    print("[loop] 首次运行：需要学习通账号（将写入本目录 .env，仅本地保存）")
    try:
        user = input("  CX_USER(手机号): ").strip()
        passwd = input("  CX_PASS(密码): ").strip()
    except EOFError:
        # stdin 关闭（管道/计划任务等非交互场景）：无法引导，如实退出
        print("\n[loop] 无交互输入流，无法引导。请先在 .env 里填好 CX_USER/CX_PASS。",
              flush=True)
        return False
    if not user or not passwd:
        print("[loop] 未提供凭据，无法开始。", flush=True)
        return False
    env_path = repo_root() / ".env"
    _merge_env_file(env_path, {"CX_USER": user, "CX_PASS": passwd})
    os.environ["CX_USER"], os.environ["CX_PASS"] = user, passwd
    print(f"[loop] 凭据已写入 {env_path}", flush=True)
    return True


def _account_banner() -> None:
    """打印本次实际使用的账号（打码）——账号错位（.env vs 环境变量残留）的第一线索。"""
    user = (os.environ.get("CX_USER") or "").strip()
    if not user:
        return
    masked = f"{user[:3]}****{user[-4:]}" if len(user) >= 8 else user
    print(f"[loop] 使用账号 {masked}（exe 旁 .env 优先于机器环境变量）", flush=True)


def _prompt_and_activate_course() -> bool:
    """提示粘贴课程 URL,校验后激活(旧课自动归档/已有进度保留,见 activate_course)。"""
    for _ in range(3):
        try:
            url = input("  course-url: ").strip()
        except EOFError:
            print("\n[loop] 无交互输入流,无法引导。请改用命令行:\n"
                  "  Xuexitong.exe --action initialize --course-url \"...\"",
                  flush=True)
            return False
        if not url:
            return False
        if _activate_course_url(url):
            return True
    return False


def _activate_course_url(url: str) -> bool:
    """解析并激活给定课程 URL（非交互）；成功返回 True。

    与 _prompt_and_activate_course 共用同一套激活路径
    （resolve_course → CourseIdentity → activate_course），不新造第二种激活方式。
    P5 的 config/courses.json 也走这里。
    """
    from resolvers.course_resolver import resolve_course
    r = resolve_course(url)
    if not r.is_ok():
        print(f"  [!] URL 解析失败:{r.error};请重新复制完整 URL。", flush=True)
        return False
    from datetime import timezone
    from state.course_state import CourseIdentity, activate_course
    ident_dict = r.to_dict()["identity"]
    identity = CourseIdentity(
        course_id=ident_dict["course_id"], clazz_id=ident_dict["clazz_id"],
        cpi=ident_dict["cpi"], title=ident_dict.get("title", ""),
        raw_url=url, resolved_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    activate_course(identity)
    print(f"[loop] 课程已激活:{identity.key()}", flush=True)
    return True


def _ensure_active_course() -> bool:
    """确保有活跃课程。激活顺序（个人自用改造 P5）：

      1. 已有活跃课程 → 直接通过
      2. config/courses.json 里第一个 enabled 且 URL 非空的课程 → 非交互自动激活
      3. 都没有 → 交互引导粘贴 URL（上游行为，保留 s 换课能力）

    注：config/courses.json 是本改造新增的、**真正会被读取**的配置。上游那份
    config/course.json 是死配置 —— 全仓库没有任何代码读它（实际活跃课程存在
    state/accounts/<账号哈希>/active_course.json）。
    """
    from state.course_state import load_active_course
    if load_active_course():
        return True

    # ② 配置文件优先：把 URL 填进 config/courses.json 即可免去每次粘贴
    try:
        from utils.personal_config import first_enabled_course
        item = first_enabled_course()
    except Exception as e:
        item = None
        print(f"[loop] 读取 config/courses.json 失败（非致命）：{e}", flush=True)
    if item:
        url = (item.get("course_url") or "").strip()
        name = (item.get("name") or "").strip() or url[:48]
        print(f"[loop] 只跑第 1 门：{name}（来自 config/courses.json）", flush=True)
        if _activate_course_url(url):
            return True
        print("[loop] ⚠️ config/courses.json 里的 URL 激活失败，回落到手动粘贴。",
              flush=True)

    # ③ 交互引导（上游行为）
    print("[loop] 尚未配置课程。请到学习通打开目标课程的章节学习页,"
          "从浏览器地址栏完整复制 URL(需含 chapterId/courseId/clazzid/cpi/enc/"
          "hidetype/openc)后粘贴。")
    return _prompt_and_activate_course()


# ── 启动向导(可跳过:回车全默认) ────────────────────────────────────

def _env_max_chapters() -> "int | None":
    """XUE_LOOP_MAX_CHAPTERS 的合法值(≥1 整数),未设/非法返回 None。"""
    raw = (os.environ.get("XUE_LOOP_MAX_CHAPTERS") or "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    return val if val >= 1 else None


def _parse_max_chapters(raw: str, current: int) -> int:
    """向导输入解析:空 = 保持 current;非法/<1 = 保持 current。"""
    raw = (raw or "").strip()
    if not raw:
        return current
    try:
        val = int(raw)
    except ValueError:
        return current
    return val if val >= 1 else current


def _active_course_desc() -> str:
    """当前课程一句话描述:key「标题」(已完成 x/y 章)。"""
    from state.course_state import load_active_course, load_course_state
    active = load_active_course()
    if not active:
        return "(尚未配置)"
    desc = active.key()
    title = (getattr(active, "title", "") or "").strip()
    if title:
        desc += f"「{title}」"
    prog = getattr(load_course_state(active.key()), "progress", None)
    done, total = getattr(prog, "completed", None), getattr(prog, "total", None)
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        desc += f"(已完成 {done}/{total} 章)"
    return desc


def _print_progress(result, loop_start: float) -> None:
    """每轮打印一行进度摘要（个人自用改造 P3）。

    数据源与 _active_course_desc 同源（course_state 的 progress），不新造第二套
    计数；当前章取本轮实际尝试的最后一章；「本次已用」= 本次常驻进程启动至今。
    """
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
    print(f"[进度] 已完成 {prog_s} 章 | 本轮章 {chap} | "
          f"本次已用 {hh:02d}:{mm:02d}:{ss:02d}", flush=True)


def _launch_wizard(explicit_max: "int | None") -> "int | None":
    """交互启动的跳过式向导:展示课程/进度,回车全默认,s 换课,可调本次章数。

    仅在 stdin 为 TTY 时调用;答案**只作用于本次会话**,不写回 state/.env。
    返回本次会话的 max_chapters(None = 调用方用默认 1)。
    优先级:CLI 显式 > XUE_LOOP_MAX_CHAPTERS > 向导输入 > 1。
    """
    from state.course_state import load_active_course
    if load_active_course():
        print(f"[loop] 当前课程:{_active_course_desc()}", flush=True)
    else:
        print("[loop] 尚未配置课程。请到学习通打开目标课程的章节学习页,"
              "从浏览器地址栏完整复制 URL(需含 chapterId/courseId/clazzid/"
              "cpi/enc/hidetype/openc)。", flush=True)
        _prompt_and_activate_course()
        if load_active_course():
            print(f"[loop] 当前课程:{_active_course_desc()}", flush=True)

    max_chapters = explicit_max if explicit_max is not None else _env_max_chapters()
    hint = "回车直接开始;s=换课"
    if explicit_max is None and _env_max_chapters() is None:
        print(f"[loop] 每轮推进章数 max_chapters:{max_chapters or 1}"
              "(输入 2/3/… 仅本次会话生效)", flush=True)
        hint = "输入章数 / s=换课 / 回车直接开始"
    try:
        raw = input(f"[loop] {hint}: ").strip()
    except EOFError:
        return max_chapters
    if raw.lower() == "s":
        print("[loop] 换课(旧课自动归档、进度保留;换错可再换回):", flush=True)
        _prompt_and_activate_course()
        return max_chapters
    return _parse_max_chapters(raw, max_chapters or 1)


# ── 停止信号 ────────────────────────────────────────────────────────

class _StopState:
    def __init__(self) -> None:
        self.requests = 0

    def request(self, *_) -> None:
        self.requests += 1
        if self.requests >= 2:
            # 第二次 Ctrl+C：恢复默认行为立即退出，不再等当前轮
            raise KeyboardInterrupt


def _sleep_interruptible(total_s: float, stop: _StopState) -> None:
    """分段 sleep，期间响应停止请求并每分钟报一次剩余时间。"""
    deadline = time.monotonic() + total_s
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0 or stop.requests:
            return
        tick = min(60.0, remain)
        time.sleep(min(tick, 1.0))
        if stop.requests:
            return
        if remain > 90 and abs(remain % 60) < 1.0:
            print(f"[loop] 下一轮倒计时 {int(remain // 60)} 分钟…"
                  "（Ctrl+C 优雅停止）", flush=True)


# ── 运行时段闸门（个人自用改造 P4）──────────────────────────────────
# .env 里设 XUE_LOOP_WINDOW=23:00-07:00 → 只在夜间跑，白天自动等待（不启动浏览器）。
# 未设置该变量 = 全天可跑（与上游行为一致，向后兼容）。

def _parse_window(spec: str) -> "tuple[int, int] | None":
    """解析 'HH:MM-HH:MM' → (start_min, end_min)；空/格式非法 → None（=全天）。"""
    s = (spec or "").strip()
    if not s:
        return None
    left, sep, right = s.partition("-")
    if not sep:
        return None

    def _to_min(text: str) -> int:
        hh, _, mm = text.strip().partition(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(text)
        return h * 60 + m

    try:
        return _to_min(left), _to_min(right)
    except Exception:
        return None


def _now_min() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def _in_window(now_min: int, win: "tuple[int, int] | None") -> bool:
    """是否在允许运行的时段内。

    win=None → 全天；start>end 表示跨零点（如 23:00-07:00 即「晚 11 点到次日 7 点」）。
    """
    if not win:
        return True
    start, end = win
    if start == end:
        return True                              # 起止相同 → 视为全天
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end      # 跨夜


def _seconds_until_open(now_min: int, win: "tuple[int, int]") -> int:
    """距下一个时段开始还有多少秒（至少 60 秒，避免 0 秒忙等）。"""
    delta = (win[0] - now_min) % (24 * 60)
    return max(1, delta) * 60


# ── 主循环 ──────────────────────────────────────────────────────────

def run_loop(interval_minutes: int = 0, max_chapters: "int | None" = None) -> int:
    """常驻循环入口，返回进程退出码。

    max_chapters 优先级：CLI 显式 > XUE_LOOP_MAX_CHAPTERS > 启动向导 > 默认 1。
    """
    stop = _StopState()
    import signal
    signal.signal(signal.SIGINT, stop.request)

    # 调度层的决策/异常只 print 到 stdout，exe 双击形态下关窗即丢 ——
    # 而故障高发恰恰在这一层（选错章、熔断、探针空转），引擎层日志一片 PASS
    # 时恰恰看不出「这一轮为什么没推进」。装 tee 让窗口里看到的 = 事后能查的。
    # 句柄存到局部变量并在整个函数期持有（GC 关闭文件会让日志静默截断）。
    _log_fh = None
    if not env_opt_out():
        _log_fh = install_run_log(LOG_PATH)
        if _log_fh is not None:
            from utils.version import build_info
            _bi = build_info()
            print(f"[loop] 运行日志: {LOG_PATH}"
                  f"（版本 {_bi['app_version']}"
                  f"{'/' + _bi['git_sha'] if _bi['git_sha'] else ''}）", flush=True)

    try:
        interval_min = int(interval_minutes or 0) or int(
            os.environ.get("XUE_LOOP_INTERVAL", "") or DEFAULT_INTERVAL_MIN)
    except ValueError:
        interval_min = DEFAULT_INTERVAL_MIN
    interval_min = max(1, interval_min)
    try:
        active_min = int(os.environ.get("XUE_LOOP_ACTIVE_INTERVAL", "")
                         or DEFAULT_ACTIVE_INTERVAL_MIN)
    except ValueError:
        active_min = DEFAULT_ACTIVE_INTERVAL_MIN
    active_min = max(1, active_min)

    lock = _acquire_lock(LOCK_PATH)
    if lock is None:
        print(f"[!] 已有另一个实例在运行（锁：{LOCK_PATH}）。同一账号并发会互踢"
              "会话，请勿双开。", file=sys.stderr, flush=True)
        return 2

    try:
        print(f"[loop] 本地常驻模式：空闲轮每 {interval_min} 分钟、活跃轮每 "
              f"{active_min} 分钟（state 根：{repo_root()}）", flush=True)
        if not _ensure_credentials():
            return 2
        _account_banner()
        try:
            interactive = bool(sys.stdin and sys.stdin.isatty())
        except Exception:
            interactive = False
        if interactive:
            # 交互启动（双击/终端）：跳过式向导——回车全默认，s 换课，可调本次章数
            max_chapters = _launch_wizard(max_chapters)
            if max_chapters is None:
                max_chapters = 1
            from state.course_state import load_active_course
            if not load_active_course():
                return 2
        else:
            # 非交互（计划任务/管道）：静默沿用 CLI/env；无课程走 EOF 防御引导
            if not _ensure_active_course():
                return 2
            if max_chapters is None:
                max_chapters = _env_max_chapters() or 1

        from scheduler import run_scheduler
        error_streak = 0
        cycles = 0
        # 个人自用改造：本次常驻起点（供进度行显示「本次已用」）+ 运行时段闸门配置
        loop_start = time.time()
        _window_spec = (os.environ.get("XUE_LOOP_WINDOW") or "").strip()
        _win = _parse_window(_window_spec)
        if _window_spec:
            if _win:
                print(f"[loop] 运行时段限定为 {_window_spec}"
                      f"（当前{'在' if _in_window(_now_min(), _win) else '不在'}"
                      f"时段内）", flush=True)
            else:
                print(f"[loop] ⚠️ XUE_LOOP_WINDOW 格式无法解析：{_window_spec!r}"
                      f"（应形如 23:00-07:00），本次按全天处理。", flush=True)
        while not stop.requests:
            # 时段闸门（P4）：不在允许时段内 → 睡到时段开启再跑，期间不启动浏览器
            if not _in_window(_now_min(), _win):
                _wait_s = _seconds_until_open(_now_min(), _win)
                print(f"[loop] 当前不在运行时段（{_window_spec}），"
                      f"等待 {_wait_s // 60} 分钟后继续…（Ctrl+C 可退出）",
                      flush=True)
                _sleep_interruptible(_wait_s, stop)
                continue
            cycles += 1
            t0 = time.time()
            print(f"\n[loop] ── 第 {cycles} 轮 ── "
                  f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
            try:
                result = run_scheduler(None, "", "schedule",
                                       run_id=f"local-loop-{int(t0)}",
                                       max_chapters=max_chapters)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # 单轮异常不退出进程
                error_streak += 1
                print(f"[loop] 本轮异常（{error_streak}/{ERROR_STREAK_EXIT}）："
                      f"{type(e).__name__}: {e}", flush=True)
                if error_streak >= ERROR_STREAK_EXIT:
                    print("[loop] 连续异常达到上限，退出。请检查网络/凭据/课程 URL。",
                          flush=True)
                    return 1
                _sleep_interruptible(min(interval_min, 5) * 60, stop)
                continue

            decision, res, verdict = (result.decision, result.result,
                                      result.verdict or "")
            print(f"[loop] 本轮结果：decision={decision} result={res} "
                  f"verdict={verdict or '-'} "
                  f"({round(time.time() - t0, 1)}s)", flush=True)
            _print_progress(result, loop_start)     # P3：每轮一行进度摘要

            if decision == "NOOP" and NO_PENDING_MARK in verdict:
                print("[loop] 课程已无可推进任务（完成或无可识别的视频点）。"
                      "退出常驻模式。", flush=True)
                return 0
            if decision == "NOOP" and NO_ACTIVE_COURSE_MARK in verdict:
                # 理论上 _ensure_active_course 已挡住；防御：状态被外部删掉时重建
                if not _ensure_active_course():
                    return 2
                continue
            if decision == "ERROR":
                error_streak += 1
                if error_streak >= ERROR_STREAK_EXIT:
                    print(f"[loop] 连续 {error_streak} 轮 ERROR，退出。"
                          f"最后错误：{result.error or verdict}", flush=True)
                    return 1
            else:
                error_streak = 0

            if not stop.requests:
                # 活跃轮（刚推进过任务，decision=RUN）用短间隔连续刷；
                # 空闲轮（NOOP/BLOCKED/ERROR）用长间隔，避免空转打扰服务端
                _sleep_interruptible(active_min * 60 if decision == "RUN"
                                     else interval_min * 60, stop)

        print("[loop] 收到停止请求，退出。", flush=True)
        return 0
    except KeyboardInterrupt:
        # 第二次 Ctrl+C 从信号处理器抛出 —— 静默退出，不打 traceback
        print("[loop] 强制退出。", flush=True)
        return 130
    finally:
        try:
            lock.close()
        except Exception:
            pass
        # 显式关闭日志：靠 GC 的话解释器退出时可能丢尾部几行，
        # 而「退出前的最后几行」恰恰常常就是报错本身。
        if _log_fh is not None:
            try:
                _log_fh.flush()
                _log_fh.close()
            except Exception:
                pass
