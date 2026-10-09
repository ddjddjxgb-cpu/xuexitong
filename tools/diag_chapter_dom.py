"""诊断：dump 章节页的任务点结构，判断「有无视频」的判定依据是否有效。

背景：引擎判定某章「有视频」的唯一依据是页面上存在
`.ans-insertvideo-online[objectid]` 元素（见 tvdp/tdvp.py 的 VIDEO_OID_ENUM_JS）。
当探针报告「无未完成视频工作」而你认为该章有视频时，用本脚本看页面**实际**
存在什么元素 —— 从而区分两种情况：

  A. 真的没有视频（课程以文档/实训为主）
  B. 超星改版导致选择器失效（元素换了 class 名）

用法：
    python tools/diag_chapter_dom.py                # 用 config/courses.json 的 chapterId
    python tools/diag_chapter_dom.py 1222249871     # 指定章节

会打开浏览器（可见），登录后 dump 结构并关闭。
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

# 在页面上下文里收集所有可能相关的结构
DUMP_JS = r"""() => {
    const uniq = a => Array.from(new Set(a));
    const out = {
        url: location.href,
        // A. 引擎的判定依据：视频任务点
        videoPoints: [],
        // B. 任务点图标（_classify_job 读它的 class 推断类型）
        jobMarks: [],
        // C. 所有 iframe（看视频是否在别的帧里）
        iframes: [],
        // D. 任何 class 里带 video/media 的元素（找改版后的新选择器）
        suspicious: [],
        // E. 任务点/章节条目的文本摘要
        itemTexts: [],
    };

    document.querySelectorAll('.ans-insertvideo-online').forEach(n => {
        out.videoPoints.push({oid: n.getAttribute('objectid'), cls: n.className});
    });
    document.querySelectorAll('[class*="ans-job"]').forEach(n => {
        out.jobMarks.push({cls: n.className, tag: n.tagName});
    });
    document.querySelectorAll('iframe').forEach(n => {
        out.iframes.push((n.getAttribute('src') || '(no src)').slice(0, 160));
    });
    document.querySelectorAll('[class*="video"],[class*="media"],[class*="player"]').forEach(n => {
        if (out.suspicious.length < 25) {
            out.suspicious.push({tag: n.tagName, cls: String(n.className).slice(0, 120)});
        }
    });
    document.querySelectorAll('.posCatalog_name, .chapter_item, [class*="catalog"] span').forEach(n => {
        const t = (n.textContent || '').trim();
        if (t && out.itemTexts.length < 20) out.itemTexts.push(t.slice(0, 60));
    });
    return out;
}"""


def main() -> int:
    url = first_enabled_course_url()
    if not url:
        print("✗ config/courses.json 里没有课程 URL")
        return 1
    cp = CourseParams.from_url(url)

    chapter = sys.argv[1] if len(sys.argv) > 1 else cp.chapter_id
    print(f"课程: {cp.course_id}_{cp.clazz_id}   诊断章节: {chapter}")
    print("启动浏览器…（会短暂出现窗口）")

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

        cp.chapter_id = chapter
        target = cp.build_base_url()
        ok = ensure_login(page, ctx, target, os.environ["CX_USER"],
                          os.environ["CX_PASS"], login_timeout_s=30)
        print("登录:", ok)
        if not ok:
            browser.close()
            return 1

        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)      # 给 cards iframe 渲染时间

        print(f"\n页面 URL: {page.url[:110]}")
        print(f"帧数: {len(page.frames)}")

        dumped_any = False
        for fr in page.frames:
            fu = fr.url or ""
            if "knowledge/cards" not in fu:
                continue
            dumped_any = True
            print("\n" + "═" * 66)
            print("cards 帧:", fu[:110])
            print("═" * 66)
            try:
                data = fr.evaluate(DUMP_JS)
            except Exception as e:
                print("  dump 失败:", e)
                continue
            for key, label in [
                ("videoPoints", "A. 视频任务点 .ans-insertvideo-online（引擎的判定依据）"),
                ("jobMarks", "B. 任务点图标 [class*=ans-job]（类型判定依据）"),
                ("iframes", "C. 该帧内的 iframe"),
                ("suspicious", "D. class 含 video/media/player 的元素"),
                ("itemTexts", "E. 目录条目文本"),
            ]:
                items = data.get(key) or []
                print(f"\n{label}  —— 命中 {len(items)} 个")
                for it in items[:12]:
                    print("   ", json.dumps(it, ensure_ascii=False)[:140])

        if not dumped_any:
            print("\n⚠ 没有找到 knowledge/cards 帧 —— cards iframe 未渲染。")
            print("  可能原因：URL 缺 openc/hidetype，或该章无任务点。")
            for fr in page.frames:
                print("   帧:", (fr.url or "(空)")[:110])

        browser.close()

    print("\n" + "═" * 66)
    print("判读方法：")
    print("  · A 命中 > 0        → 该章有视频，引擎应能识别")
    print("  · A 命中 = 0 且 D 有 → 可能改版换了选择器（把 D 里的 class 反馈给我）")
    print("  · A/D 都为空        → 该章确实没有视频（文档/实训类章节）")
    print("═" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
