"""因子约束分配链路测试：批次读取、缓存复用、增量重算、中断续算与版本冲突。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import factor_rules as rules
from app.store import store
from app.services.factor_allocation import (
    RECEIPT_READY,
    STRATEGY_REBUILD,
    STRATEGY_REUSE,
    FactorAllocationService,
)
from app.services.metric_def import MetricDefService

ALLOCATION_TABLES = [
    rules.CACHE_MODULE,
    rules.ALLOCATION_MODULE,
    rules.BATCH_MODULE,
    rules.ARCHIVE_MODULE,
]


def _reset():
    """重置分配相关表与因子配置，保证用例之间互不影响。"""
    for table in ALLOCATION_TABLES:
        store._tables[table] = []
    store._versions[rules.CONFIG_MODULE] = 1
    store.bump_version(rules.EXPERIMENT_MODULE)
    store.rows(rules.EXPERIMENT_MODULE)[:] = [
        {
            "id": 1, "方案编号": "EXPE-0001", "实验名称": "首页改版",
            "样本总量": "10000", "分流比例": "5:5", "status": "运行中",
        },
        {
            "id": 2, "方案编号": "EXPE-0002", "实验名称": "推荐改版",
            "样本总量": "20000", "分流比例": "6:4", "status": "运行中",
        },
        {
            "id": 3, "方案编号": "EXPE-0003", "实验名称": "模块排序",
            "样本总量": "8000", "分流比例": "7:3", "status": "运行中",
        },
    ]
    store.rows(rules.CONFIG_MODULE)[:] = [
        {
            "id": 1, "因子编号": "EXPR-0001", "因子名称": "按钮颜色", "因子层级": "UI层",
            "取值集合": "蓝色,绿色,红色", "分配比例": "5:3:2",
            "互斥规则": "同一用户只命中一种颜色", "关联实验": "EXPE-0001,EXPE-0002",
            "生效批次": "B001", "status": "已生效",
        },
        {
            "id": 2, "因子编号": "EXPR-0002", "因子名称": "推荐算法", "因子层级": "算法层",
            "取值集合": "v1,v2", "分配比例": "5:5",
            "互斥规则": "", "关联实验": "EXPE-0002,EXPE-0003",
            "生效批次": "B001", "status": "已生效",
        },
        {
            "id": 3, "因子编号": "EXPR-0003", "因子名称": "模块顺序", "因子层级": "UI层",
            "取值集合": "默认,推荐优先", "分配比例": "7:3",
            "互斥规则": "与按钮颜色实验互斥", "关联实验": "EXPE-0003",
            "生效批次": "B002", "status": "待校验",
        },
    ]
    store.reset_scan_count()


def test_full_generation_reads_each_table_once():
    """整批生成只在开头读一次实验表与因子配置表，读取次数不随行数增长。"""
    _reset()
    service = FactorAllocationService()
    result = service.generate(scope="full")
    assert store.scan_count(rules.EXPERIMENT_MODULE) == 1
    assert store.scan_count(rules.CONFIG_MODULE) == 1
    batch = result["批次"]
    assert batch["状态"] == "已完成"
    assert batch["明细总数"] == 5  # UI层3条 + 算法层2条
    assert batch["已回执数"] == 5
    assert set(batch["已完成因子组"]) == {"UI层", "算法层"}
    # 每条明细都保留缓存批次和规则版本。
    for row in store.rows(rules.ALLOCATION_MODULE):
        assert row["缓存批次"] == batch["缓存批次"]
        assert row["规则版本"]
        assert row["生成状态"] == RECEIPT_READY


def test_allocation_uses_experiment_split_ratio():
    """分配明细按实验分流比例与因子分配比例计算。"""
    _reset()
    FactorAllocationService().generate(scope="full")
    row = next(
        r for r in store.rows(rules.ALLOCATION_MODULE)
        if r["实验编号"] == "EXPE-0002" and r["因子编号"] == "EXPR-0001"
    )
    assert row["对照比例"] == "60%"
    assert row["实验比例"] == "40%"
    assert row["对照样本"] == 12000
    assert row["实验样本"] == 8000
    assert "蓝色:50%" in row["取值"]


def test_cache_and_rule_version_saved_together():
    """约束缓存与规则版本同一条落库，且缓存批次可查。"""
    _reset()
    service = FactorAllocationService()
    result = service.generate(scope="full")
    batch_no = result["批次"]["缓存批次"]
    for cache in store.rows(rules.CACHE_MODULE):
        assert cache["缓存批次"] == batch_no
        assert cache["规则版本"]
        assert cache["校验结果"]
        assert cache["失效标记"] is False
        assert cache["缓存键"] == service._cache_key(cache["因子组"], cache["因子摘要"])
    assert service.verify()["ok"] is True


def test_single_factor_change_recomputes_only_affected_group():
    """只改一个因子配置时只重算受影响组，已完成组明细与版本保持不变。"""
    _reset()
    service = FactorAllocationService()
    first = service.generate(scope="full")
    batch_no = first["批次"]["缓存批次"]
    ui_before = [dict(row) for row in store.rows(rules.ALLOCATION_MODULE) if row["因子组"] == "UI层"]
    algo_before = [dict(row) for row in store.rows(rules.ALLOCATION_MODULE) if row["因子组"] == "算法层"]

    store.find(rules.CONFIG_MODULE, 2)["分配比例"] = "6:4"
    store.bump_version(rules.CONFIG_MODULE)
    result = service.generate(scope="incremental")

    assert result["本轮重算因子组"] == ["算法层"]
    assert result["保持因子组"] == ["UI层"]
    # 同一批次的缓存批次号不变，UI层明细逐字段保持不变。
    assert result["批次"]["缓存批次"] == batch_no
    ui_after = [dict(row) for row in store.rows(rules.ALLOCATION_MODULE) if row["因子组"] == "UI层"]
    assert ui_before == ui_after
    algo_after = [dict(row) for row in store.rows(rules.ALLOCATION_MODULE) if row["因子组"] == "算法层"]
    assert algo_before[0]["规则版本"] != algo_after[0]["规则版本"]
    # UI层的缓存批次仍然可以按批次号查到。
    ui_cache = next(row for row in store.rows(rules.CACHE_MODULE) if row["因子组"] == "UI层")
    assert ui_cache["缓存批次"] == batch_no
    assert service.verify()["ok"] is True


def test_update_factor_skips_recompute_when_unchanged():
    """配置字段没有变化时不触发重算。"""
    _reset()
    service = FactorAllocationService()
    service.generate(scope="full")
    result = service.update_factor(1, {"因子名称": "按钮颜色"})
    assert result["变更"] is False
    assert result["重算结果"] is None


def test_conflict_reuse_keeps_batch_and_marks_compensated():
    """版本冲突时复用旧缓存：分配表保留旧批次，被补算的实验单列。"""
    _reset()
    service = FactorAllocationService()
    first = service.generate(scope="full")
    batch_no = first["批次"]["缓存批次"]

    store.find(rules.CONFIG_MODULE, 1)["取值集合"] = "蓝色,绿色"
    store.bump_version(rules.CONFIG_MODULE)
    conflict = service.detect_conflict()
    assert conflict["冲突"] is True
    assert conflict["变更因子组"] == ["UI层"]
    assert set(conflict["补算实验"]) == {"EXPE-0001", "EXPE-0002", "EXPE-0003"}

    result = service.generate(scope="incremental", strategy=STRATEGY_REUSE)
    assert result["批次"]["缓存批次"] == batch_no
    assert set(result["批次"]["补算实验"]) == {"EXPE-0001", "EXPE-0002", "EXPE-0003"}
    compensated = [
        row for row in store.rows(rules.ALLOCATION_MODULE)
        if row["因子组"] == "UI层" and row["补算标记"] == "补算"
    ]
    assert compensated
    # 算法层不受影响，规则版本原样保留。
    assert service.verify()["ok"] is True


def test_conflict_rebuild_archives_old_cache_and_batch():
    """清空缓存整批重建：生成新版本批次，旧缓存差异与旧明细留档。"""
    _reset()
    service = FactorAllocationService()
    first = service.generate(scope="full")
    old_batch = first["批次"]["缓存批次"]

    store.find(rules.CONFIG_MODULE, 1)["分配比例"] = "4:4:2"
    store.bump_version(rules.CONFIG_MODULE)
    result = service.generate(scope="full", strategy=STRATEGY_REBUILD)

    new_batch = result["批次"]["缓存批次"]
    assert new_batch != old_batch
    assert result["留档"] == old_batch
    archive = store.rows(rules.ARCHIVE_MODULE)[0]
    assert archive["归档批次"] == old_batch
    assert archive["明细"]
    assert archive["缓存差异"]
    # 旧缓存被打上失效标记，新缓存全部有效。
    invalid = [row for row in store.rows(rules.CACHE_MODULE) if row["失效标记"]]
    valid = [row for row in store.rows(rules.CACHE_MODULE) if not row["失效标记"]]
    assert invalid and valid
    assert {row["缓存批次"] for row in valid} == {new_batch}
    assert service.verify()["ok"] is True


def test_interrupt_then_retry_only_fills_pending_groups():
    """中断后按未完成组补算，已完成组不回退。"""
    _reset()
    service = FactorAllocationService()
    result = service.generate(scope="full", simulate_failure="算法层")
    batch = result["批次"]
    assert batch["状态"] == "部分完成"
    assert batch["因子组状态"]["UI层"]["状态"] == "已完成"
    assert batch["因子组状态"]["算法层"]["状态"] == "未完成"
    ui_snapshot = [dict(row) for row in store.rows(rules.ALLOCATION_MODULE)]
    assert len(ui_snapshot) == 3
    assert service.verify()["ok"] is True

    retried = service.generate(scope="retry")
    assert retried["本轮重算因子组"] == ["算法层"]
    assert retried["批次"]["状态"] == "已完成"
    assert retried["批次"]["重试次数"] == 1
    ui_after = [dict(row) for row in store.rows(rules.ALLOCATION_MODULE) if row["因子组"] == "UI层"]
    assert ui_snapshot == ui_after
    assert len(store.rows(rules.ALLOCATION_MODULE)) == 5
    assert service.verify()["ok"] is True


def test_interrupted_group_transaction_leaves_no_residue():
    """中断的因子组不留下半套缓存或明细（事务整体回滚）。"""
    _reset()
    service = FactorAllocationService()
    service.generate(scope="full", simulate_failure="算法层")
    assert not [
        row for row in store.rows(rules.ALLOCATION_MODULE) if row["因子组"] == "算法层"
    ]
    assert not [
        row for row in store.rows(rules.CACHE_MODULE) if row["因子组"] == "算法层"
    ]
    assert service.verify()["ok"] is True


def test_metric_def_consumes_allocation_table():
    """V015 跨模块联动：指标定义读取实验编号、缓存批次与因子层。"""
    _reset()
    service = FactorAllocationService()
    batch_no = service.generate(scope="full")["批次"]["缓存批次"]
    view = MetricDefService().factor_layers()
    assert view["缓存批次"] == batch_no
    layers = {item["因子层级"]: item for item in view["因子层"]}
    assert set(layers) == {"UI层", "算法层"}
    assert layers["UI层"]["实验编号"] == ["EXPE-0001", "EXPE-0002", "EXPE-0003"]
    assert layers["算法层"]["实验编号"] == ["EXPE-0002", "EXPE-0003"]
    assert layers["UI层"]["缓存批次"] == batch_no


def test_consumer_view_exposes_batch_and_layers():
    """消费链路返回分配表的实验编号、缓存批次与规则版本。"""
    _reset()
    service = FactorAllocationService()
    batch_no = service.generate(scope="full")["批次"]["缓存批次"]
    view = service.consumer_view()
    assert view["缓存批次"] == batch_no
    assert view["因子层"] == ["UI层", "算法层"]
    assert view["覆盖实验"] == ["EXPE-0001", "EXPE-0002", "EXPE-0003"]
    assert all(item["缓存批次"] == batch_no for item in view["明细"])


def test_invalid_factor_validation_is_reported_not_dropped():
    """取值数与比例段数不一致时仍生成明细，但标记待复核并给出原因。"""
    _reset()
    store.find(rules.CONFIG_MODULE, 1)["分配比例"] = "5:3"
    service = FactorAllocationService()
    service.generate(scope="full")
    row = next(
        r for r in store.rows(rules.ALLOCATION_MODULE) if r["因子编号"] == "EXPR-0001"
    )
    assert row["生成状态"] == "待复核"
    assert "不一致" in row["约束校验"]


def test_missing_experiment_is_reported():
    """关联实验不存在时批次头给出缺失清单，不阻断其他实验分配。"""
    _reset()
    store.find(rules.CONFIG_MODULE, 2)["关联实验"] = "EXPE-0002,EXPE-9999"
    result = FactorAllocationService().generate(scope="full")
    assert result["缺失实验"] == ["EXPE-9999"]
    assert result["批次"]["缺失实验"] == ["EXPE-9999"]


def test_full_regeneration_does_not_mark_compensation():
    """整批重生成沿用同一条冲突信息时不把全部实验标成补算。"""
    _reset()
    service = FactorAllocationService()
    service.generate(scope="full")
    store.find(rules.CONFIG_MODULE, 1)["取值集合"] = "蓝色,绿色"
    store.bump_version(rules.CONFIG_MODULE)
    result = service.generate(scope="full")
    assert result["批次"]["补算实验"] == []
    assert all(row["补算标记"] == "无" for row in store.rows(rules.ALLOCATION_MODULE))
    assert service.verify()["ok"] is True


def test_factor_layer_change_clears_old_group_residue():
    """因子改层级后，旧因子组在增量重算里被清掉，不会残留两套组。"""
    _reset()
    service = FactorAllocationService()
    service.generate(scope="full")
    store.find(rules.CONFIG_MODULE, 2)["因子层级"] = "服务层"
    store.bump_version(rules.CONFIG_MODULE)
    result = service.generate(scope="incremental")
    groups = {row["因子组"] for row in store.rows(rules.ALLOCATION_MODULE)}
    assert "算法层" not in groups
    assert {"UI层", "服务层"} <= groups
    assert "服务层" in result["本轮重算因子组"]
    assert service.verify()["ok"] is True


def test_retry_after_first_group_failure_reuses_same_batch():
    """第一个组就中断时，重试找回同一批次号，不另起一批。"""
    _reset()
    service = FactorAllocationService()
    first = service.generate(scope="full", simulate_failure="UI层")
    batch_no = first["批次"]["缓存批次"]
    assert first["批次"]["状态"] == "已中断"
    assert first["批次"]["明细总数"] == 0
    assert service.verify()["ok"] is True

    retried = service.generate(scope="retry")
    assert retried["批次"]["缓存批次"] == batch_no
    assert retried["批次"]["状态"] == "已完成"
    assert retried["本轮重算因子组"] == ["UI层", "算法层"]
    assert len(store.rows(rules.ALLOCATION_MODULE)) == 5
    assert service.verify()["ok"] is True
