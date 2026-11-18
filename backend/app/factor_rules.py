"""因子约束规则的纯函数实现：分流比例、取值集合、互斥约束、因子组与版本摘要。

这里刻意不依赖 store / seed，方便「种子数据」和「分配服务」共用同一套口径：
同一份因子配置在两处算出的因子摘要、规则版本必须一致，否则缓存复用会被误判为失效。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

DEFAULT_GROUP = "默认因子组"
DEFAULT_RATIO = (5.0, 5.0)

# 因子配置可写字段：分配服务改配置时只认这些字段，避免把状态字段写坏。
FACTOR_FIELDS = [
    "因子编号", "因子名称", "因子层级", "取值集合",
    "分配比例", "互斥规则", "关联实验", "生效批次",
]

# 分配链路涉及的表名集中在这里，避免服务层和跨模块读取方各写一份字符串。
CONFIG_MODULE = "expr_factor"
EXPERIMENT_MODULE = "experiment"
CACHE_MODULE = "factor_constraint_cache"
ALLOCATION_MODULE = "factor_allocation"
BATCH_MODULE = "factor_allocation_batch"
ARCHIVE_MODULE = "factor_allocation_archive"


def group_key(factor: dict[str, Any]) -> str:
    """因子组按因子层级归并；层级为空时落到默认组。"""
    return str(factor.get("因子层级") or "").strip() or DEFAULT_GROUP


def group_factors(factors: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把因子配置按层级分组，保持首次出现顺序，便于稳定出批次。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for factor in factors:
        grouped.setdefault(group_key(factor), []).append(factor)
    return grouped


def experiment_codes(factor: dict[str, Any]) -> list[str]:
    """读取因子关联的实验编号，支持中英文逗号、顿号与分号分隔。"""
    raw = str(factor.get("关联实验") or "")
    for separator in ("，", "、", ";", "；", " "):
        raw = raw.replace(separator, ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def factor_fingerprint(factor: dict[str, Any]) -> str:
    """单条因子配置的稳定摘要：变化即代表需要重算该因子所在的组。"""
    payload = "|".join([
        str(factor.get("因子编号") or ""),
        str(factor.get("因子层级") or ""),
        str(factor.get("取值集合") or ""),
        str(factor.get("分配比例") or ""),
        str(factor.get("互斥规则") or ""),
        ",".join(experiment_codes(factor)),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def group_fingerprint(factors: Iterable[dict[str, Any]]) -> str:
    """一个因子组的整体摘要，用于判断约束缓存能否复用。"""
    digests = sorted(factor_fingerprint(factor) for factor in factors)
    return hashlib.sha1("|".join(digests).encode("utf-8")).hexdigest()[:12]


def rule_version(fingerprint: str) -> str:
    """规则版本只跟随「本组因子摘要」，不受其他组改动影响。

    这一点是增量重算的关键：只改一个因子配置时，未受影响因子组的规则版本保持不变，
    已完成的分配和缓存批次才可以原样复用。
    """
    return f"RV-{fingerprint}"


def combined_rule_version(versions: Iterable[str]) -> str:
    """分配表级别的规则版本：各组版本合并，便于整表追溯。"""
    joined = "|".join(sorted(set(versions)))
    return f"RV-ALL-{hashlib.sha1(joined.encode('utf-8')).hexdigest()[:8]}"


def split_ratio(text: Any) -> tuple[float, float]:
    """解析分流比例，异常或非正数时退回 5:5，保证分配不因脏数据中断。"""
    raw = str(text or "").strip()
    parts = [part for part in raw.replace("：", ":").split(":") if part.strip()]
    try:
        values = [float(part) for part in parts[:2]]
    except (TypeError, ValueError):
        return DEFAULT_RATIO
    if not values or any(value <= 0 for value in values):
        return DEFAULT_RATIO
    if len(values) == 1:
        values.append(values[0])
    return values[0], values[1]


def ratio_text(control: float, experiment: float) -> str:
    """把权重还原成页面使用的比例文案，去掉无意义的 .0。"""

    def tidy(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else f"{value:g}"

    return f"{tidy(control)}:{tidy(experiment)}"


def normalize_values(text: Any) -> list[str]:
    """拆分取值集合，保持配置顺序。"""
    raw = str(text or "")
    for separator in ("，", "、", ";", "；", "|"):
        raw = raw.replace(separator, ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def allocation_weights(text: Any) -> list[float]:
    """解析因子内部分配比例，无法解析时返回空列表由校验环节给出原因。"""
    raw = str(text or "").strip()
    parts = [part for part in raw.replace("：", ":").split(":") if part.strip()]
    if not parts:
        return []
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return []
    return values if all(value > 0 for value in values) else []


def validate_constraints(factor: dict[str, Any]) -> tuple[str, str]:
    """一次性校验取值集合、分配比例与互斥规则，返回可读结果与约束摘要。"""
    values = normalize_values(factor.get("取值集合"))
    weights = allocation_weights(factor.get("分配比例"))
    if not values:
        return "不通过：取值集合为空", "取值集合为空"
    if not weights:
        return "不通过：分配比例格式无效", "分配比例格式无效"
    if len(values) != len(weights):
        return (
            f"不通过：取值数 {len(values)} 与分配比例段数 {len(weights)} 不一致",
            f"取值数 {len(values)} / 比例段数 {len(weights)} 不一致",
        )
    exclusive = str(factor.get("互斥规则") or "").strip()
    total = sum(weights)
    shares = "、".join(
        f"{value}={weight / total:.0%}" for value, weight in zip(values, weights)
    )
    summary = f"取值 {len(values)} 个；{shares}"
    if exclusive:
        summary = f"{summary}；互斥：{exclusive}"
    return "通过：约束可分配", summary


def allocation_snapshot(experiment: dict[str, Any], factor: dict[str, Any]) -> dict[str, Any]:
    """按实验分流比例和因子分配比例计算单条明细的落库字段。"""
    control_weight, experiment_weight = split_ratio(experiment.get("分流比例"))
    total_weight = control_weight + experiment_weight
    control_share = control_weight / total_weight if total_weight else 0.5
    experiment_share = experiment_weight / total_weight if total_weight else 0.5
    try:
        total_sample = int(str(experiment.get("样本总量") or "").strip())
    except (TypeError, ValueError):
        total_sample = 10000
    if total_sample <= 0:
        total_sample = 10000
    control_sample = int(total_sample * control_share)
    experiment_sample = total_sample - control_sample

    values = normalize_values(factor.get("取值集合"))
    weights = allocation_weights(factor.get("分配比例"))
    if values and weights and len(values) == len(weights):
        value_total = sum(weights)
        assignment = "、".join(
            f"{value}:{weight / value_total:.0%}" for value, weight in zip(values, weights)
        )
    else:
        assignment = str(factor.get("分配比例") or "")

    return {
        "分流比例": ratio_text(control_weight, experiment_weight),
        "对照比例": f"{control_share:.0%}",
        "实验比例": f"{experiment_share:.0%}",
        "对照样本": control_sample,
        "实验样本": experiment_sample,
        "取值": assignment,
        "分配比例": str(factor.get("分配比例") or ""),
        "因子摘要": factor_fingerprint(factor),
    }
