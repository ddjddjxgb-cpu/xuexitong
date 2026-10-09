"""诊断：点击章节页的标签页，观察任务点/视频是否随之出现。

背景：本课程（信息技术基础类）的章节页是「标签页」结构 ——
任务导读 / 图谱导航 / 教学资源1 / 教学资源2 / 测验习题 / 巩固提升。
引擎读的 cards iframe 默认只加载第一个标签（任务导读，纯文字），
因此 point-read 恒定 pts=0。

本脚本回答：**点击后面的标签，任务点会不会出现？** 从而判断
  A. 可修 —— 引擎加一步「先点标签」即可
  B. 不可修 —— 标签内容是异步/跨域加载，Playwright 拿不到

用法：
    python tools/diag_tabs.py 1222249934
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, ".")

from utils.stdio_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from utils.env_file import load_env_file        # noqa: E402
from utils.paths import repo_root               # noqa: E402

load_env_file(repo_root(), override=True)

from models import CourseParams                                  # noqa: E402
from utils.browser_factory import display_args, launch_kwargs    # noqa: E402
from utils.cookie_store import ensure_login                      # noqa: E402
from utils.personal_config import first_enabled_course_url       # noqa: E402

# 统计某帧里与「任务点」相关的元素（引擎关心的是前两个）
COUNT_JS = r"""() => ({
    insertvideo: document.querySelectorAll('.ans-insertvideo-online').length,
    job_icon: document.querySelectorAll('[class*="ans-job"]').length,
    video_el: document.querySelectorAll('video').length,
    iframe_n: document.querySelectorAll('iframe').length,
    title: document.title,
    bodyLen: (document.body ? document.body.innerHTML.length : 0),
    // 该帧内 iframe 的 src（视频可能在下一层）
    iframes: Array.from(document.querySelectorAll('iframe'))
                  .map(x => (x.getAttribute('src') || '').slice(0, 110)).slice(0, 6),
})"""

# 找主页面上的标签元素
TABS_JS = r"""() => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('div,span,a,li').forEach(n => {
        const t = (n.textContent || '').trim();
        // 标签文本都很短，且是这几个已知名字
        if (t && t.length <= 8 && /(任务导读|图谱导航|教学资源|测验习题|巩固提升|任务导学|知识图谱|章节测验)/.test(t)) {
            const key = t + '|' + (n.parentElement ? n.parentElement.className : '');
            if (seen.has(key)) return;
            seen.add(key);
            const cs = window.getComputedStyle(n);
            out.push({
                text: t,
                tag: n.tagName,
                cls: String(n.className || '').slice(0, 70),
                clickable: cs.cursor === 'pointer',
                parentCls: String(n.parentElement ? n.parentElement.className : '').slice(0, 70),
            });
        }
    });
    return out;
}"""


def snap(page, label):
    """打印当前所有帧的任务点统计。"""
    print(f"\n  ── {label} ──")
    for i, fr in enumerate(page.frames):
        fu = fr.url or ""
        if "knowledge/cards" not in fu and "about:blank" in fu:
            continue
        try:
            d = fr.evaluate(COUNT_JS)
        except Exception:
            continue
        tag = "cards" if "knowledge/cards" in fu else "主帧"
        print(f"    [{tag}] title={d['title'][:14]:<14} "
              f"insertvideo={d['insertvideo']} ans-job={d['job_icon']} "
              f"video={d['video_el']} iframe={d['iframe_n']} bodyLen={d['bodyLen']}")
        if d["insertvideo"] or d["video_el"]:
            print(f"        ★★★ 发现任务点/视频！iframes={d['iframes']}")
        elif d["iframes"]:
            print(f"        子 iframe: {d['iframes']}")


def main() -> int:
    url = first_enabled_course_url()
    cp = CourseParams.from_url(url)
    chapter = sys.argv[1] if len(sys.argv) > 1 else cp.chapter_id
    cp.chapter_id = chapter

    print(f"课程 {cp.course_id}_{cp.clazz_id}   章节 {chapter}")

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
        if not ensure_login(page, ctx, target, os.environ["CX_USER"],
                            os.environ["CX_PASS"], login_timeout_s=30):
            print("登录失败")
            browser.close()
            return 1

        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        snap(page, "初始加载后")

        tabs = page.evaluate(TABS_JS)
        print(f"\n  在主页面上找到 {len(tabs)} 个标签元素：")
        for t in tabs:
            print("   ", json.dumps(t, ensure_ascii=False)[:150])

        # 依次点击每个标签，观察 cards 帧变化
        for idx, t in enumerate(tabs):
            label = t["text"]
            print(f"\n{'=' * 70}")
            print(f"  点击第 {idx + 1} 个标签：「{label}」")
            try:
                loc = page.get_by_text(label, exact=True).first
                loc.click(timeout=5000)
            except Exception as e:
                print(f"    点击失败: {type(e).__name__}: {str(e)[:90]}")
                continue
            page.wait_for_timeout(4500)
            snap(page, f"点击「{label}」后")

        print(f"\n{'=' * 70}")
        print("判读：")
        print("  · 若某个标签点击后 insertvideo 或 video > 0 → 可修：引擎加一步点击即可")
        print("  · 若始终为 0 → 任务点在 Playwright 取不到的层（跨域/异步），本工具无法处理")
        print("=" * 70)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
