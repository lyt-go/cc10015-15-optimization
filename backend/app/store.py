"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 只服务于因子分配链路的内部表：不当作独立业务模块出现在运营概览里。
INTERNAL_MODULES = {
    "factor_constraint_cache",
    "factor_allocation_batch",
    "factor_allocation_archive",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._versions: dict[str, int] = {name: 1 for name in self._tables}
        self._created_at: dict[str, float] = {name: time.time() for name in self._tables}
        # 读取计数器：分配生成时用它确认「复用约束缓存」确实没有重复扫实验/因子配置。
        self._scans: dict[str, int] = {}

    def module_names(self) -> list[str]:
        return [name for name in sorted(self._tables) if name not in INTERNAL_MODULES]

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def read_rows(self, module: str) -> list[dict[str, Any]]:
        """批量读取一份模块快照并记一次扫描。

        分配链路只允许在批次开头读一次实验表和一次因子配置表；后续因子组按批次复用
        这份内存快照，不再逐条回表。扫描计数用于回归测试断言读取次数有上界。
        """
        self._scans[module] = self._scans.get(module, 0) + 1
        return self._tables.setdefault(module, [])

    def scan_count(self, module: str | None = None) -> int:
        if module is None:
            return sum(self._scans.values())
        return self._scans.get(module, 0)

    def reset_scan_count(self) -> None:
        self._scans.clear()

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def filter_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def bump_version(self, module: str) -> int:
        self._versions[module] = self._versions.get(module, 0) + 1
        return self._versions[module]

    def version(self, module: str) -> int:
        return self._versions.get(module, 1)

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """整组提交的事务边界：中途抛异常就把相关表、版本号和创建时间原样回滚。

        因子约束分配表要求「约束缓存」和「规则版本」同时落库；批次分配中断时不能留下
        半套缓存/版本，也不能让重试覆盖已完成组，所以这里按模块做快照回滚。
        """
        snapshot = {
            name: (
                [copy.deepcopy(row) for row in self._tables.get(name, [])],
                self._versions.get(name, 1),
                self._created_at.get(name),
            )
            for name in modules
        }
        try:
            yield
        except Exception:
            for name, (rows, version, created_at) in snapshot.items():
                if name in self._tables:
                    self._tables[name] = rows
                else:
                    self._tables.pop(name, None)
                self._versions[name] = version
                if created_at is None:
                    self._created_at.pop(name, None)
                else:
                    self._created_at[name] = created_at
            raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
