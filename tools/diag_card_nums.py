"""诊断：遍历 cards 的 num 参数，找出每个标签页里的任务点类型。

背景（本课程的结构）：
    章节页 = 标签页组，每页对应 cards 接口的一个 num 值：
        num=0 任务导读 / num=1 图谱导航 / num=2 教学资源1 /
        num=3 教学资源2 / num=4 测验习题 / num=5 巩固提升
    引擎目前只读默认的 num=0（任务导读，纯文字），故 point-read 恒 pts=0。

本脚本直接按 num 访问 cards 接口，逐个报告：
    · 该页标题
    · 是否含视频任务点（.ans-insertvideo-online / video / ans-job）
    · 含哪些可识别的任务点特征

用法：
    python tools/diag_card_nums.py 1222249934 [max_num]
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

COUNT_JS = r"""() => {
    const q = s => document.querySelectorAll(s).length;
    return {
        title: document.title,
        insertvideo: q('.ans-insertvideo-online'),
        job_icon: q('[class*="ans-job"]'),
        video_el: q('video'),
        videoCls: q('[class*="video"]'),
        iframe_n: q('iframe'),
        bodyLen: document.body ? document.body.innerHTML.length : 0,
        // 任务点图标的具体 class（用于分类）
        jobClasses: Array.from(document.querySelectorAll('[class*="ans-job"]'))
                        .map(n => String(n.className)).slice(0, 8),
        // 视频点的 objectid
        oids: Array.from(document.querySelectorAll('.ans-insertvideo-online'))
                 .map(n => n.getAttribute('objectid')).slice(0, 8),
        // 文本摘要（判断页面性质：文档？测验？）
        texts: Array.from(document.querySelectorAll('div,span,h1,h2,h3,p'))
                    .map(n => (n.textContent || '').trim())
                    .filter(t => t && t.length < 30).slice(0, 12),
        // 该页内的子 iframe
        iframes: Array.from(document.querySelectorAll('iframe'))
                      .map(x => (x.getAttribute('src') || '').slice(0, 100)).slice(0, 5),
    };
}"""


def main() -> int:
    url = first_enabled_course_url()
    cp = CourseParams.from_url(url)
    chapter = sys.argv[1] if len(sys.argv) > 1 else cp.chapter_id
    max_num = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    # cards 接口的 URL 模板（从帧 URL 里实测得到，见 diag_tabs 的输出）
    def cards_url(n: int) -> str:
        return ("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards"
                f"?clazzid={cp.clazz_id}&courseid={cp.course_id}"
                f"&knowledgeid={chapter}&num={n}&ut=s&cpi={cp.cpi}")

    print(f"课程 {cp.course_id}_{cp.clazz_id}   章节 {chapter}")
    print(f"遍历 num=0..{max_num}\n")

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

        # 先正常进章节页拿到登录态（cards 接口需要会话）
        cp.chapter_id = chapter
        target = cp.build_base_url()
        if not ensure_login(page, ctx, target, os.environ["CX_USER"],
                            os.environ["CX_PASS"], login_timeout_s=30):
            print("登录失败")
            browser.close()
            return 1
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        hits = []
        for n in range(0, max_num + 1):
            u = cards_url(n)
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                print(f"  num={n}  导航失败: {type(e).__name__}")
                continue
            page.wait_for_timeout(3000)
            try:
                d = page.evaluate(COUNT_JS)
            except Exception as e:
                print(f"  num={n}  evaluate 失败: {type(e).__name__}")
                continue

            mark = ""
            if d["insertvideo"] or d["video_el"]:
                mark = "  ★★★ 有视频！"
                hits.append((n, d))
            print(f"  num={n}  title={d['title'][:12]:<12} "
                  f"insertvideo={d['insertvideo']} ans-job={d['job_icon']} "
                  f"video={d['video_el']} iframe={d['iframe_n']} "
                  f"bodyLen={d['bodyLen']}{mark}")
            if d["jobClasses"]:
                print(f"          jobClasses: {d['jobClasses']}")
            if d["oids"]:
                print(f"          video objectids: {d['oids']}")
            if d["iframes"]:
                print(f"          子 iframe: {d['iframes']}")
            print(f"          文本样本: {d['texts'][:8]}")

        print(f"\n{'=' * 70}")
        if hits:
            print("  ★ 找到含视频的页：")
            for n, d in hits:
                print(f"    num={n}  title={d['title']}  objectids={d['oids']}")
            print("\n  → 结论：可改造。引擎需先切到对应 num 再读任务点。")
        else:
            print("  未在 num=0..%d 中发现视频元素。" % max_num)
            print("  → 可能：该章确实无视频；或视频在更深的 num / 子 iframe 里。")
        print("=" * 70)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
