"""page_ops — 页面级操作的单一实现（跨模块复用）。

为什么单独一个模块：
    「切换 cards 卡片页（标签页）」这个动作，**播放引擎**（app/e2_headed_gha.py，
    负责按 num 播放）和**探针**（tvdp/tdvp.py，负责按 num 扫描）都要用。
    放在这里避免两处各写一份、日后行为漂移。

背景（新版课程的结构）：
    一个 chapterId 下，`/mooc-ans/knowledge/cards` 接口有多个 `num` 页 ——
    0=任务导读 / 1=图谱导航 / 2,3=教学资源 / 4=测验习题 / 5=巩固提升 …
    任务点（含视频）分散在各页里，而**页面默认只加载 num=0**，
    它通常是纯文字导读，一个任务点都没有 → 会误判「本章无视频」。

    实测（tools/diag_tabs.py、tools/verify_card_switch.py）：点击标签的本质
    就是把 cards iframe 的 src 换成带对应 num 的 URL，无需模拟点击。
"""

from __future__ import annotations


def switch_card_num(page, num) -> bool:
    """把 cards iframe 切到指定 num 页；返回是否发出了切换动作。

    num <= 0 → **不做任何事**并返回 True —— 这让「不切换」成为默认行为，
    对上游的单页结构（cards 里直接列全部任务点）零影响。

    返回 True 只表示切换动作已发出，**不保证内容已渲染** ——
    调用方需自行等待（引擎等 3s，探针按帧出现判断）。

    找不到 cards iframe、或 iframe 无 src 时返回 False（调用方可据此判定
    「切不了」，回退到不切的原有行为）。
    """
    try:
        n = int(num or 0)
    except (TypeError, ValueError):
        return True
    if n <= 0:
        return True
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
        }""", n))
    except Exception:
        return False


def cards_frame_url_num(page):
    """读当前 cards iframe 的 num（诊断用）；取不到返回 None。"""
    try:
        return page.evaluate("""() => {
            const ifr = document.querySelector('iframe[src*="knowledge/cards"]');
            if (!ifr) return null;
            const m = (ifr.getAttribute('src') || '').match(/[?&]num=(\\d+)/);
            return m ? parseInt(m[1], 10) : null;
        }""")
    except Exception:
        return None
