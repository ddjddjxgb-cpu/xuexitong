"""改进项 A1/A3 的回归网（2026-10-09）。

A1 排序自然序：task_id 末键从字典序改为数字段按数值比较 —— 单章 ≥10 个视频时
':video10' 不再插队到 ':video2' 前面。纯函数 + reconcile_queue 集成两层钉住。

A3 UI 证据体检 TTL：COMPLETED(UI) 超过 7 天且从未被点级复核的降级为 STALE
（触发 rollback 优先路径 → 下轮成为 head 吃 E6.2 live 复核：真完成治愈回
SERVER_VERIFIED 不重播，真漏被确认）。SERVER_VERIFIED/RECHECK 不体检；
无时间戳的保守不动；now_utc 注入可测。
"""

import pytest
from datetime import datetime, timedelta, timezone

import app.registry.task_registry as R
from app.registry.task_registry import TaskRecord, reconcile_queue
from app.registry.reconcile import (
    stale_ui_completed_for_recheck,
    UI_EVIDENCE_RECHECK_TTL_DAYS,
)


# ── A1：_task_id_natural_key ───────────────────────────────────────

def test_natural_key_orders_video10_after_video2():
    """暗雷本体：字典序 '1222249871:video10' < '1222249871:video2'，自然序必须反过来。"""
    ids = ["1222249871:video2", "1222249871:video10"]
    assert sorted(ids) == ["1222249871:video10", "1222249871:video2"], \
        "前提自检：字典序确实会把 video10 排到 video2 前面"
    assert sorted(ids, key=R._task_id_natural_key) == \
        ["1222249871:video2", "1222249871:video10"]


def test_natural_key_full_sequence():
    """1..12 的完整自然序，含无后缀的第 1 点与混合段（cid 本身是长数字）。"""
    ids = [f"1222249871:video{i}" for i in range(2, 13)]
    ids = ["1222249871"] + ids
    got = sorted(ids, key=R._task_id_natural_key)
    assert got == ids, "第 1 点(cid) 最前，其后 video2..video12 严格递增"


def test_natural_key_mixed_segments_stable():
    """非数字段与数字段混排可比（int 永不与 str 直接比较）。"""
    ids = ["c:other", "c:video3", "c:video10", "c"]
    got = sorted(ids, key=R._task_id_natural_key)
    assert got[0] == "c" and got[-1] in ("c:other", "c:video3", "c:video10")
    assert got.index("c:video3") < got.index("c:video10")


def test_reconcile_queue_uses_natural_order(monkeypatch, tmp_path):
    """集成：同章 12 个视频任务按自然序入队（monkeypatch 掉 registry IO）。"""
    monkeypatch.setattr(R, "load_registry", lambda key: {})
    monkeypatch.setattr(R, "save_queue", lambda key, q: None)
    reg = {}
    for i in range(1, 13):
        tid = "1222249871" if i == 1 else f"1222249871:video{i}"
        t = TaskRecord(tid, "1222249871", "任务1.3", task_type="video",
                       status="DISCOVERED")
        t._ch_idx = 3
        t._cell_idx = 3
        reg[tid] = t
    q = reconcile_queue("course", reg, set(), points_map={})
    got = [it["task_id"] for it in q.items]
    assert got.index("1222249871:video2") < got.index("1222249871:video10"), \
        "reconcile_queue 的排序末键必须是自然序"


# ── A3：stale_ui_completed_for_recheck ─────────────────────────────

_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def _ui_completed_record(tid, cid, verified_days_ago):
    t = TaskRecord(tid, cid, "T", task_type="video", status="COMPLETED")
    t.verification.level = "UI"
    t.verification.verified_at_utc = (
        _NOW - timedelta(days=verified_days_ago)).isoformat()
    t.completion_evidence.type = "UI"
    t.completion_evidence.source = "server DOM completed marker"
    return t


def test_ttl_constant_is_seven_days():
    assert UI_EVIDENCE_RECHECK_TTL_DAYS == 7


def test_expired_ui_evidence_is_flagged():
    t = _ui_completed_record("c1", "c1", verified_days_ago=8)
    out = stale_ui_completed_for_recheck({"c1": t}, now_utc=_NOW)
    assert out == ["c1"], "UI 证据超过 7 天 → 进体检清单"


def test_fresh_ui_evidence_is_kept():
    t = _ui_completed_record("c1", "c1", verified_days_ago=3)
    assert stale_ui_completed_for_recheck({"c1": t}, now_utc=_NOW) == []


def test_server_verified_evidence_never_rechecked():
    t = _ui_completed_record("c1", "c1", verified_days_ago=400)
    t.verification.level = "SERVER_VERIFIED"
    t.completion_evidence.type = "SERVER_VERIFIED"
    t.completion_evidence.passed_object_ids = ["obj-1"]
    assert stale_ui_completed_for_recheck({"c1": t}, now_utc=_NOW) == [], \
        "点级细真源不需要 DOM 级体检"


def test_non_completed_ignored():
    t = _ui_completed_record("c1", "c1", verified_days_ago=30)
    t.status = "PENDING"
    assert stale_ui_completed_for_recheck({"c1": t}, now_utc=_NOW) == []


def test_missing_timestamp_is_conservative():
    """无时间戳 → 无过期依据 → 不降（保守方向：宁可多信一次，不无据翻案）。"""
    t = TaskRecord("c1", "c1", "T", task_type="video", status="COMPLETED")
    t.verification.level = "UI"
    t.verification.verified_at_utc = None
    t.completion_evidence.type = "UI"
    assert stale_ui_completed_for_recheck({"c1": t}, now_utc=_NOW) == []


def test_naive_timestamp_treated_as_utc():
    t = _ui_completed_record("c1", "c1", verified_days_ago=8)
    t.verification.verified_at_utc = (
        _NOW - timedelta(days=8)).replace(tzinfo=None).isoformat()
    assert stale_ui_completed_for_recheck({"c1": t}, now_utc=_NOW) == ["c1"], \
        "旧数据可能无 tzinfo，按 UTC 解释不得崩溃"
