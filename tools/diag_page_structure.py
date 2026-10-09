"""诊断（全面版）：侦察一个章节页在**所有帧**里的真实结构。

用途：当 point-read 报告 `pts=0`（cards 帧里没有任何任务点）时，本脚本回答：
  · 任务点/视频到底在哪个帧、哪个容器里？
  · 是不是「标签页」式结构（需要点击标签才加载内容）？
  · 引擎当前读的 cards 帧里实际有什么？

用法：
    python tools/diag_page_structure.py 1222249934
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

# 每帧跑一次：统计各类元素 + 收集标签页/任务点的可辨认特征
PROBE_FRAME_JS = r"""() => {
    const q = s => document.querySelectorAll(s).length;
    const out = {
        url: location.href,
        title: document.title,
        counts: {
            video_tag: q('video'),
            insertvideo: q('.ans-insertvideo-online'),
            job_icon: q('[class*="ans-job"]'),
            iframe: q('iframe'),
            // 引擎目前用的候选容器
            cards: q('[class*="cards"]'),
            // 任务点/章节条目常见类名
            chapter_item: q('[class*="chapter_item"]'),
            posCatalog: q('[class*="posCatalog"]'),
            jobUnfinish: q('[class*="jobUnfinish"]'),
            // 视频/媒体类
            videoCls: q('[class*="video"]'),
            mediaCls: q('[class*="media"]'),
            // 标签页
            tab: q('[class*="tab"],[role="tab"]'),
        },
        // 把 class 名收集起来，便于人工比对改版后的命名
        sampleClasses: [],
        // 页面上可见的短文本（帮助判断这是哪个界面）
        texts: [],
    };
    // 收集带 class 的元素名（去重、限量）
    const seen = new Set();
    document.querySelectorAll('*[class]').forEach(n => {
        const c = String(n.className || '');
        c.split(/\s+/).forEach(x => { if (x) seen.add(x); });
    });
    out.sampleClasses = Array.from(seen).slice(0, 150);
    // 文本样本
    document.querySelectorAll('div,span,a,li').forEach(n => {
        const t = (n.textContent || '').trim();
        if (t && t.length < 24 && out.texts.length < 40) out.texts.push(t);
    });
    return out;
}"""


def main() -> int:
    url = first_enabled_course_url()
    if not url:
        print("✗ config/courses.json 没有课程 URL")
        return 1
    cp = CourseParams.from_url(url)
    chapter = sys.argv[1] if len(sys.argv) > 1 else cp.chapter_id
    cp.chapter_id = chapter

    print(f"课程 {cp.course_id}_{cp.clazz_id}   侦察章节 {chapter}")
    print("启动浏览器…")

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
        ok = ensure_login(page, ctx, target, os.environ["CX_USER"],
                          os.environ["CX_PASS"], login_timeout_s=30)
        print("登录:", ok)
        if not ok:
            browser.close()
            return 1

        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(15000)      # 给标签页/卡片充分加载时间

        print(f"\n页面 URL: {page.url[:120]}")
        print(f"总帧数: {len(page.frames)}")
        print("═" * 70)

        for i, fr in enumerate(page.frames):
            fu = fr.url or "(about:blank)"
            try:
                d = fr.evaluate(PROBE_FRAME_JS)
            except Exception as e:
                print(f"\n[帧 {i}] {fu[:100]}")
                print(f"   evaluate 失败: {type(e).__name__}")
                continue
            c = d["counts"]
            # 只详细打印"有内容"的帧
            interesting = (c["video_tag"] or c["insertvideo"] or c["job_icon"]
                           or c["chapter_item"] or c["videoCls"] or c["tab"])
            print(f"\n[帧 {i}] {(fu[:100])}")
            print(f"   title: {d['title'][:70]}")
            print(f"   计数: video={c['video_tag']} insertvideo={c['insertvideo']} "
                  f"ans-job={c['job_icon']} iframe={c['iframe']} "
                  f"cards={c['cards']} chapter_item={c['chapter_item']} "
                  f"videoCls={c['videoCls']} tab={c['tab']}")
            if interesting:
                print(f"   ★ 该帧有内容，class 样本（前 60 个）：")
                print("     " + " ".join(d["sampleClasses"][:60]))
            if c["tab"] or c["videoCls"]:
                print(f"   文本样本: {d['texts'][:15]}")

        print("\n" + "═" * 70)
        print("判读：")
        print("  · 哪个帧的 insertvideo / video 计数 > 0 → 视频就在那个帧")
        print("  · 若只有 tab 有内容而 insertvideo=0 → 标签页结构，需点击标签才加载")
        print("  · 把 ★ 行的 class 样本发我，可比对引擎选择器是否失配")
        print("═" * 70)

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
