"""验证：切到指定 card num 后，视频能否真正播放（改造可行性终点验证）。

背景：本课程章节是标签页结构，视频在 cards 接口的 num=2/3 页里，
而引擎只读默认的 num=0。本脚本验证「切 num → 找到点 → 起播 → currentTime 增长」
这条链路是否成立 —— 成立则改造可行。

它**复用引擎自己的函数**（enumerate_video_objectids / activate_target_point /
get_video_state / set_playback_rate），因此结论可直接外推到引擎。

用法：
    python tools/verify_card_switch.py <chapterId> <num> [video_index]
    例：python tools/verify_card_switch.py 1222249934 2 1
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, ".")

from utils.stdio_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from utils.env_file import load_env_file        # noqa: E402
from utils.paths import repo_root               # noqa: E402

load_env_file(repo_root(), override=True)

from app.e2_headed_gha import (                                        # noqa: E402
    activate_target_point,
    enumerate_video_objectids,
    get_video_state,
    set_playback_rate,
)
from models import CourseParams                                        # noqa: E402
from utils.browser_factory import display_args, launch_kwargs          # noqa: E402
from utils.cookie_store import ensure_login                            # noqa: E402


def switch_card_num(page, num: int) -> bool:
    """把 cards iframe 的 src 切到指定 num —— 等价于点击对应标签页。

    实测（tools/diag_tabs.py）：点击标签后 cards iframe 的 URL 由
    `...&num=0&...` 变为 `...&num=1&...`，即点击的本质就是换 iframe 的 src。
    """
    try:
        return bool(page.evaluate("""(n) => {
            const ifr = document.querySelector('iframe[src*="knowledge/cards"]');
            if (!ifr) return false;
            const raw = ifr.getAttribute('src') || '';
            if (!raw) return false;
            let neu;
            if (/[?&]num=\\d+/.test(raw)) {
                neu = raw.replace(/([?&])num=\\d+/, '$1num=' + n);
            } else {
                neu = raw + (raw.indexOf('?') >= 0 ? '&' : '?') + 'num=' + n;
            }
            ifr.setAttribute('src', neu);
            return true;
        }""", num))
    except Exception as e:
        print("  switch 失败:", type(e).__name__, e)
        return False


def cards_frame(page):
    """返回当前 cards 帧（切换 num 后帧会被替换，需重新取）。"""
    for fr in page.frames:
        if "knowledge/cards" in (fr.url or ""):
            return fr
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    chapter = sys.argv[1]
    target_num = int(sys.argv[2])
    video_index = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    cp = CourseParams.from_url(os.environ.get("_XUE_URL", "")) if os.environ.get("_XUE_URL") else None
    if cp is None:
        from utils.personal_config import first_enabled_course_url
        cp = CourseParams.from_url(first_enabled_course_url())
    cp.chapter_id = chapter

    print(f"课程 {cp.course_id}_{cp.clazz_id}   章节 {chapter}   目标 num={target_num}   视频序号={video_index}")
    print("=" * 70)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, **launch_kwargs(),
            args=[*display_args(None), "--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-web-security",
                  "--disable-site-isolation-trials"],
        )
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        target = cp.build_base_url()

        # ── 1. 登录并进入章节页 ──
        ok = ensure_login(page, ctx, target, os.environ["CX_USER"],
                          os.environ["CX_PASS"], login_timeout_s=30)
        print("① 登录:", ok)
        if not ok:
            browser.close()
            return 1
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        f0 = cards_frame(page)
        print("② 初始 cards 帧:", (f0.url[:90] + "…") if f0 else "未找到")

        # ── 2. 切到目标 num ──
        if not switch_card_num(page, target_num):
            print("③ ✗ 未能切换 cards iframe 的 src")
            browser.close()
            return 1
        page.wait_for_timeout(6000)
        f1 = cards_frame(page)
        print("③ 切换后 cards 帧:", (f1.url[:90] + "…") if f1 else "未找到")
        if f1 is None:
            print("   ✗ 切换后找不到 cards 帧")
            browser.close()
            return 1
        try:
            title = f1.evaluate("() => document.title")
            n_iv = f1.evaluate("() => document.querySelectorAll('.ans-insertvideo-online').length")
            print(f"   帧标题: {title}   视频点: {n_iv}")
        except Exception as e:
            print("   读取帧失败:", type(e).__name__)

        # ── 3. 枚举视频点（用引擎的函数）──
        oids = enumerate_video_objectids(page)
        print(f"④ enumerate_video_objectids → {len(oids)} 个: {oids}")
        if not oids:
            print("   ✗ 切换后仍无视频点 —— 该 num 里没有视频")
            browser.close()
            return 1

        target_oid = oids[video_index - 1] if video_index <= len(oids) else oids[0]
        print(f"   选用 objectid = {target_oid}")

        # ── 4. 激活目标点（引擎的做法：点击它的播放键）──
        moved = activate_target_point(page, target_oid)
        print(f"⑤ activate_target_point → {moved}")

        # ── 5. 等视频元数据（duration）就绪 ──
        print("⑥ 等待视频元数据…")
        dur = None
        for i in range(40):
            page.wait_for_timeout(1000)
            st = get_video_state(page, target_oid)
            if st.get("found") and st.get("duration"):
                dur = st["duration"]
                print(f"   ✓ duration={dur:.0f}s  ct={st.get('currentTime', 0):.1f}s "
                      f"rs={st.get('readyState')} paused={st.get('paused')} "
                      f"（等待 {i + 1}s）")
                break
            if i % 5 == 4:
                print(f"   …{i + 1}s 仍在等待（found={st.get('found')} reason={st.get('reason')}）")
        if not dur:
            print("   ✗ 未拿到 duration")
            browser.close()
            return 1

        # ── 6. 设倍速并起播 ──
        rate = float(os.environ.get("XUE_PLAYBACK_RATE") or 2)
        r_ok = set_playback_rate(page, target_oid, rate)
        print(f"⑦ set_playback_rate({rate}) → {r_ok}")

        # ⑦.5 真正起播：视频在**子帧**里，键盘事件到不了它 —— 必须直接对该帧调 play()。
        # 项目自带 R-04 机制（resume_paused_video）正是干这个的，这里复用，
        # 以保证验证结论能直接外推到引擎行为。
        from app.e2_headed_gha import resume_paused_video
        rp = resume_paused_video(page, target_oid)
        print(f"⑦.5 resume_paused_video → {rp}")

        # ── 7. 观察 currentTime 是否增长（真正的判据）──
        print("⑧ 观察 20 秒，看 currentTime 是否增长：")
        t0 = time.time()
        first_ct = None
        samples = []
        for _ in range(10):
            page.wait_for_timeout(2000)
            st = get_video_state(page, target_oid)
            ct = st.get("currentTime")
            pr = st.get("playbackRate")
            paused = st.get("paused")
            note = ""
            if paused:
                # 与引擎 R-04 同构：发现暂停就再推一次（浏览器自动播放策略/站点
                # 自身的暂停逻辑都可能把它按回去）
                rr = resume_paused_video(page, target_oid)
                note = f"   ← 再次 resume={rr}"
            if first_ct is None and ct:
                first_ct = ct
            samples.append(ct)
            print(f"   +{time.time() - t0:4.0f}s  ct={ct}  rate={pr}  paused={paused}{note}")

        played = [s for s in samples if s]
        growth = (max(played) - min(played)) if len(played) >= 2 else 0

        print("=" * 70)
        if growth > 1.0:
            print(f"✅ 链路成立：currentTime 增长 {growth:.1f}s（经过约 20 秒真实时间）")
            print(f"   objectid={target_oid}  duration={dur:.0f}s  rate={rate}")
            print("   → 改造可行。下一步把「切 num」接入引擎与探针。")
        else:
            print(f"❌ currentTime 未明显增长（{growth:.1f}s）—— 起播环节还有障碍")
            print("   把上面的输出发我，需要进一步诊断（可能是激活方式或播放器加载差异）")
        print("=" * 70)

        page.wait_for_timeout(3000)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
