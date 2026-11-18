"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app import factor_rules as rules
from app.store import store

MODULE = "metric_def"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    def factor_layers(self, batch_no: str | None = None) -> dict[str, Any]:
        """V015 跨模块联动：读取分配表中的实验编号、缓存批次与因子层。

        指标口径要按「因子层」核对，因此这里只消费分配表已回执的明细，不回表读因子配置；
        同一实验在分配表里保留的缓存批次与规则版本可以逐条追溯。
        """
        target = batch_no or next(
            (
                str(batch.get("缓存批次"))
                for batch in sorted(
                    store.rows(rules.BATCH_MODULE),
                    key=lambda r: int(r.get("id", 0)),
                    reverse=True,
                )
                if batch.get("状态") in {"生成中", "部分完成", "已完成"}
            ),
            "",
        )
        rows = [
            row for row in store.rows(rules.ALLOCATION_MODULE)
            if row.get("分配批次") == target
        ]
        by_layer: dict[str, dict[str, Any]] = {}
        for row in rows:
            layer = str(row.get("因子层级") or "")
            bucket = by_layer.setdefault(layer, {
                "因子层级": layer,
                "实验编号": [],
                # 分配批次是整组提交的批次头；缓存批次是这条明细实际复用的约束缓存来源。
                "缓存批次": row.get("缓存批次", ""),
                "分配批次": target,
                "规则版本": row.get("规则版本", ""),
                "明细数": 0,
                "待复核数": 0,
            })
            bucket["明细数"] += 1
            if row.get("生成状态") != "已回执":
                bucket["待复核数"] += 1
            code = str(row.get("实验编号") or "")
            if code and code not in bucket["实验编号"]:
                bucket["实验编号"].append(code)
        return {
            "缓存批次": target,
            "分配批次": target,
            "因子层": [by_layer[layer] for layer in sorted(by_layer)],
            "覆盖实验": sorted({str(row.get("实验编号") or "") for row in rows} - {""}),
            "明细总数": len(rows),
        }
