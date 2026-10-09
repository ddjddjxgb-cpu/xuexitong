"""personal_config — 个人课程列表（自用改造 P5）。

为什么需要它：上游的 `config/course.json` 是**死配置** —— 全仓库没有任何代码
读取它（实际活跃课程存在 `state/accounts/<账号哈希>/active_course.json`）。
本模块新增一个**真正会被读取**的课程列表 `config/courses.json`。

结构设计为列表以预留多课程；**当前只消费第一个 enabled 且 course_url 非空的
条目**（不实现多课程轮刷调度 —— 需要时再加，属独立功能）。
"""

from __future__ import annotations

import json
from typing import Optional

from utils.paths import repo_root

CONFIG_PATH = repo_root() / "config" / "courses.json"


def load_courses() -> list[dict]:
    """读 `config/courses.json` 的 courses 列表。

    failure-safe：文件缺失 / JSON 语法错 / 结构不符，一律返回空列表 ——
    由调用方回落到交互引导，绝不因为一个配置文件把常驻循环打挂。
    同时兼容「顶层直接是数组」的写法。
    """
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("courses") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def first_enabled_course() -> Optional[dict]:
    """返回第一个 `enabled`（缺省视为 True）且 `course_url` 非空的条目；无则 None。"""
    for item in load_courses():
        if not item.get("enabled", True):
            continue
        if (item.get("course_url") or "").strip():
            return item
    return None


def first_enabled_course_url() -> Optional[str]:
    """便捷封装：只取 URL 字符串；无则 None。"""
    item = first_enabled_course()
    if not item:
        return None
    return (item.get("course_url") or "").strip() or None
