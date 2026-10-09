"""课程 URL 签名参数的剥离 / 注入（state 提交前脱敏、运行前注入的双向闸门）。

背景（2026-10-09 GHA 实测）：超星 studentstudy 页面强制校验 enc 签名参数，
缺失时页面直接返回「enc校验失败」——课程目录树无法提取，探测 PROBE_EMPTY。
而 enc/openc 属于签名参数，按最小暴露原则不入公共仓库。

用法：
  python scripts/sanitize_state.py                # strip：剥离所有 courses/*.json
                                                  # 里 raw_url 的 enc/openc（commit 前调用）
  python scripts/sanitize_state.py --inject URL   # inject：把 URL 里的 enc/openc 合并进
                                                  # courseId 匹配的 courses/*.json（运行前调用）

原则：
- enc/openc = 签名参数，无登录会话时单独泄漏不可用，但按最小暴露原则不提交。
- courseId/clazzid/cpi 是课程地址标识而非凭据，正常入库。
- 幂等：strip 对无签名文件无操作；inject 对签名缺失的 URL 才补。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

SIGNED_KEYS = ("enc", "openc")


def _strip_url(url: str) -> str:
    parts = urlsplit(url)
    qs = [(k, v) for k, v in parse_qs(parts.query, keep_blank_values=True).items()
          for v in v if k not in SIGNED_KEYS]
    return urlunsplit(parts._replace(query=urlencode(qs)))


def _inject_url(raw_url: str, signed_url: str) -> "str | None":
    """把 signed_url 里的签名参数合并进 raw_url。课程不匹配返回 None。"""
    sq = parse_qs(urlsplit(signed_url).query, keep_blank_values=True)
    rq = parse_qs(urlsplit(raw_url).query, keep_blank_values=True)
    if rq.get("courseId", sq.get("courseId")) != sq.get("courseId"):
        return None
    if rq.get("clazzid", sq.get("clazzId")) != sq.get("clazzid") and rq.get("clazzid") != sq.get("clazzid"):
        return None
    merged = {k: v for k, v in rq.items() if k not in SIGNED_KEYS}
    for k in SIGNED_KEYS:
        if sq.get(k):
            merged[k] = sq[k]
    flat = urlencode([(k, v[0]) for k, v in merged.items()])
    return urlunsplit(urlsplit(raw_url)._replace(query=flat))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", metavar="SIGNED_URL", default=None,
                    help="把该 URL 里的 enc/openc 合并进 courseId 匹配的 courses/*.json")
    args = ap.parse_args()

    targets = sorted(Path("state/accounts").glob("*/courses/*.json"))
    if not targets:
        print("[sanitize] state/accounts/*/courses/*.json 不存在，无事可做")
        return 0

    injected = stripped = skipped = 0
    for p in targets:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[sanitize] 跳过不可解析 {p}: {e}", file=sys.stderr)
            skipped += 1
            continue
        ident = d.get("course_identity") or {}
        raw = ident.get("raw_url") or ""
        if not raw:
            skipped += 1
            continue
        if args.inject:
            new = _inject_url(raw, args.inject)
            if new is None:
                print(f"[sanitize] {p.name}: courseId 不匹配注入 URL，跳过")
                skipped += 1
                continue
            if new != raw:
                ident["raw_url"] = new
                p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                injected += 1
        else:
            new = _strip_url(raw)
            if new != raw:
                ident["raw_url"] = new
                p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                stripped += 1

    mode = "inject" if args.inject else "strip"
    print(f"[sanitize] {mode}: 注入={injected} 剥离={stripped} 跳过={skipped} / 共 {len(targets)} 文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
