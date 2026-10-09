"""校验学习通课程 URL 是否可用于本项目（个人自用工具）。

为什么需要它：项目要求的不是「课程页」URL，而是「章节学习页（studentstudy）」
URL，必须同时含 7 个参数。粘错了 URL 的表现是引擎如实报 NO_CARDS_IFRAME 或
参数缺失——对不熟悉的人很难自查。本工具把这件事变成一句话结论。

用法：
    python tools/check_course_url.py "https://..."
    python tools/check_course_url.py          # 不带参数则提示粘贴

退出码：0 = 可用；1 = 不可用。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")   # 从任意位置调用都能 import 到项目模块

from utils.stdio_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from models import CourseParams  # noqa: E402

# 项目实际需要的参数（见 CourseParams.build_base_url 与 README「快速开始」）
REQUIRED = [
    ("course_id", "courseId", "课程 ID"),
    ("clazz_id", "clazzid", "班级 ID（注意：必须小写拼写）"),
    ("cpi", "cpi", "课程上下文"),
    ("enc", "enc", "服务端签名参数"),
    ("chapter_id", "chapterId", "起始章节 ID"),
]
OPTIONAL = [
    ("openc", "openc", "卡片 iframe 渲染开关（缺了会报 NO_CARDS_IFRAME）"),
    ("hidetype", "hidetype", "同上，缺了会报 NO_CARDS_IFRAME"),
]

BAR = "─" * 66


def check(url: str) -> int:
    url = (url or "").strip().strip('"').strip("'")
    if not url:
        print("  ✗ 没有拿到 URL。")
        return 1

    print(BAR)
    print("输入的 URL：")
    print("  " + (url[:120] + ("…" if len(url) > 120 else "")))
    print(BAR)

    cp = CourseParams.from_url(url)
    missing_required: list[str] = []
    missing_optional: list[str] = []

    print("必需参数：")
    for attr, param, desc in REQUIRED:
        val = getattr(cp, attr, None)
        if val:
            shown = str(val)
            if attr == "enc":                       # 签名参数打码显示
                shown = shown[:8] + "…" + shown[-4:]
            print(f"  ✓ {param:<12} = {shown:<28} {desc}")
        else:
            print(f"  ✗ {param:<12} {'(缺失)':<28} {desc}")
            missing_required.append(param)

    print("渲染开关（强烈建议有）：")
    for attr, param, desc in OPTIONAL:
        val = getattr(cp, attr, None)
        if val:
            print(f"  ✓ {param:<12} = {val:<28} {desc}")
        else:
            print(f"  ✗ {param:<12} {'(缺失)':<28} {desc}")
            missing_optional.append(param)

    print(BAR)

    if not missing_required and not missing_optional:
        print("  ✅ 这个 URL 可用。")
        print("     把它填进 config/courses.json 的 course_url 即可。")
        print(BAR)
        return 0

    if not missing_required:
        print("  ⚠ 基本可用，但缺渲染开关。")
        print(f"     缺：{'、'.join(missing_optional)}")
        print("     可能报 NO_CARDS_IFRAME（章节卡片不渲染）。")
        print("     建议仍去点一下章节页、把完整地址复制过来。")
        print(BAR)
        return 1

    print("  ❌ 这个 URL 不能用。")
    print(f"     缺必需参数：{'、'.join(missing_required)}")
    print()
    print("  怎么拿到正确的 URL：")
    print("     1. 浏览器登录学习通，进入你的课程")
    print("     2. 点左侧目录里的【某一章】（不要停在课程首页/门户页）")
    print("     3. 这时地址栏会出现含 studentstudy 与 chapterId 的地址")
    print("     4. 把那一整段地址复制过来")
    print()
    print("  提示：项目要的是「章节学习页」URL，不是课程首页，也不是课程门户页。")
    print(BAR)
    return 1


def main() -> int:
    if len(sys.argv) > 1:
        return check(" ".join(sys.argv[1:]))
    print(BAR)
    print("  学习通课程 URL 校验（个人自用）")
    print(BAR)
    try:
        url = input("  请粘贴你在学习通里复制的 URL，然后回车：\n  > ")
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消。")
        return 1
    return check(url)


if __name__ == "__main__":
    sys.exit(main())
