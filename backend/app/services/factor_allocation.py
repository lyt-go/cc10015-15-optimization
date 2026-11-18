"""因子约束分配服务：按批次复用已校验约束，生成因子约束分配表。

设计要点（对应 V015 核对口径）：

* 读取：一批分配只在开头读一次实验表、一次因子配置表；同批次内所有因子组复用同一份
  快照，不再逐条回表，实验规模变大时读取次数不随行数增长。
* 缓存：``factor_constraint_cache`` 以「因子组 + 因子摘要」为键保存校验结果和规则版本，
  约束缓存与规则版本必须同一条落库，否则同一实验会被两套规则重新解释。
* 分配表：``factor_allocation_batch`` 是整组一次提交的批次头，``factor_allocation`` 是
  逐条回执的明细，两者都保留缓存批次号与规则版本，供指标定义等下游模块读取。
* 增量：按因子组的摘要判断受影响范围，只重算变化的组；已完成组的行原样保留。
* 中断续算：每个因子组单独提交，批次头记录各组的完成状态；重试只补未完成组，
  已完成组既不重算也不回退。
* 版本冲突：冲突时可选择「复用旧缓存并补算变更实验」或「清空缓存整批重建」；
  前一做法保留旧缓存结果并把补算实验单列，后一做法整批重建并保留旧缓存差异。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

from app import factor_rules as rules
from app.store import store

# 批次状态：整组提交后按明细逐条回执，中途失败停在「已中断」。
BATCH_RUNNING = "生成中"
BATCH_PARTIAL = "部分完成"
BATCH_DONE = "已完成"
BATCH_FAILED = "已中断"
BATCH_ARCHIVED = "已归档"
ACTIVE_BATCH_STATES = (BATCH_RUNNING, BATCH_PARTIAL, BATCH_DONE)

# 复用策略：缓存刷新与提交之间发生版本冲突时的两种处理方式。
STRATEGY_REUSE = "reuse"
STRATEGY_REBUILD = "rebuild"
STRATEGY_LABELS = {STRATEGY_REUSE: "复用旧缓存并补算", STRATEGY_REBUILD: "清空缓存后整批重建"}

# 事务边界：缓存、分配明细、批次头、留档必须一起提交或一起回滚。
TRANSACTION_MODULES = (
    rules.CACHE_MODULE,
    rules.ALLOCATION_MODULE,
    rules.BATCH_MODULE,
    rules.ARCHIVE_MODULE,
)

# 单条明细可回执的状态：校验通过直接回执，未通过留给下一轮复核。
RECEIPT_READY = "已回执"
RECEIPT_REVIEW = "待复核"

DETAIL_FIELDS = [
    "分配批次", "实验编号", "实验名称", "因子编号", "因子名称", "因子层级", "因子组", "取值",
    "分配比例", "对照比例", "实验比例", "对照样本", "实验样本", "约束校验", "约束摘要",
    "缓存批次", "缓存键", "规则版本", "因子摘要", "生成状态", "补算标记",
]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _next_value(module: str, field: str) -> int:
    rows = store.rows(module)
    return max((int(row.get(field, 0) or 0) for row in rows), default=0) + 1


class FactorAllocationService:
    # ================================================================ 批次查询
    def _active_batch(self) -> dict[str, Any] | None:
        """取最近一个有效批次（生成中/部分完成/已完成）。"""
        for batch in sorted(store.rows(rules.BATCH_MODULE), key=lambda r: int(r.get("id", 0)), reverse=True):
            if batch.get("状态") in ACTIVE_BATCH_STATES:
                return batch
        return None

    def _latest_batch(self) -> dict[str, Any] | None:
        """取最近一个批次，包含中断/归档之外的失败批次。

        续算要能找回「最先失败、尚无任何已完成组」的那一批，否则重试会另起一批、
        丢失中断批次号。
        """
        for batch in sorted(store.rows(rules.BATCH_MODULE), key=lambda r: int(r.get("id", 0)), reverse=True):
            if batch.get("状态") != BATCH_ARCHIVED:
                return batch
        return None

    def _batch_by_no(self, batch_no: str) -> dict[str, Any] | None:
        for batch in store.rows(rules.BATCH_MODULE):
            if batch.get("缓存批次") == batch_no:
                return batch
        return None

    def _batch_details(self, batch_no: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(rules.ALLOCATION_MODULE)
            if row.get("分配批次") == batch_no
        ]

    def _latest_cache_batch(self) -> str:
        for row in sorted(store.rows(rules.CACHE_MODULE), key=lambda r: int(r.get("id", 0)), reverse=True):
            if not row.get("失效标记"):
                batch = row.get("最近使用批次") or row.get("缓存批次")
                if batch:
                    return str(batch)
        return ""

    def _batch_payload(self, batch: dict[str, Any]) -> dict[str, Any]:
        state = copy.deepcopy(batch.get("因子组状态") or {})
        details = self._batch_details(str(batch.get("缓存批次", "")))
        completed = [name for name, item in state.items() if item.get("状态") == "已完成"]
        pending = [name for name, item in state.items() if item.get("状态") != "已完成"]
        ready = sum(1 for row in details if row.get("生成状态") == RECEIPT_READY)
        return {
            "批次号": batch.get("缓存批次"),
            "分配批次": batch.get("缓存批次"),
            "缓存批次": batch.get("缓存批次"),
            "状态": batch.get("状态"),
            "范围": batch.get("范围"),
            "复用策略": batch.get("复用策略"),
            "复用策略说明": STRATEGY_LABELS.get(str(batch.get("复用策略")), ""),
            "规则版本": batch.get("规则版本"),
            "明细总数": len(details),
            "已回执数": ready,
            "待复核数": len(details) - ready,
            "已完成因子组": completed,
            "未完成因子组": pending,
            "缓存批次集合": sorted({str(row.get("缓存批次")) for row in details if row.get("缓存批次")}),
            "因子组状态": state,
            "补算实验": batch.get("补算实验", []),
            "缺失实验": batch.get("缺失实验", []),
            "重试次数": batch.get("重试次数", 0),
            "生成时间": batch.get("生成时间"),
            "更新时间": batch.get("更新时间"),
        }

    def status(self) -> dict[str, Any]:
        batch = self._active_batch()
        cache_rows = store.rows(rules.CACHE_MODULE)
        return {
            "批次": self._batch_payload(batch) if batch else None,
            "缓存": {
                "条数": len(cache_rows),
                "有效条数": sum(1 for row in cache_rows if not row.get("失效标记")),
                "最新缓存批次": self._latest_cache_batch(),
            },
            "配置版本": store.version(rules.CONFIG_MODULE),
        }

    # ================================================================ 读取与分组
    def _load_snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """一批分配只在开头读一次实验表、一次因子配置表，后续按组复用。"""
        experiments = [dict(row) for row in store.read_rows(rules.EXPERIMENT_MODULE)]
        factors = [dict(row) for row in store.read_rows(rules.CONFIG_MODULE)]
        return experiments, factors

    @staticmethod
    def _experiment_index(experiments: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for experiment in experiments:
            code = str(experiment.get("方案编号") or "").strip()
            if code:
                index[code] = experiment
        return index

    def _plan_groups(
        self, factors: list[dict[str, Any]], experiments: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """按因子组算本轮计划：摘要、规则版本、约束校验与命中的实验编号。"""
        experiment_codes = {str(row.get("方案编号") or "").strip() for row in experiments}
        plan: dict[str, dict[str, Any]] = {}
        for group, members in rules.group_factors(factors).items():
            fingerprint = rules.group_fingerprint(members)
            validations = {
                str(factor.get("因子编号") or factor.get("id")): rules.validate_constraints(factor)
                for factor in members
            }
            codes: list[str] = []
            for factor in members:
                for code in rules.experiment_codes(factor):
                    if code in experiment_codes and code not in codes:
                        codes.append(code)
            plan[group] = {
                "group": group,
                "factors": members,
                "fingerprint": fingerprint,
                # 规则版本只跟随本组摘要：只改一个因子时其他组版本不变。
                "rule_version": rules.rule_version(fingerprint),
                "config_version": store.version(rules.CONFIG_MODULE),
                "validations": validations,
                "experiment_codes": codes,
            }
        return plan

    @staticmethod
    def _missing_experiments(
        plan: dict[str, dict[str, Any]], experiment_codes: set[str]
    ) -> list[str]:
        missing: list[str] = []
        for item in plan.values():
            for factor in item["factors"]:
                for code in rules.experiment_codes(factor):
                    if code not in experiment_codes and code not in missing:
                        missing.append(code)
        return missing

    # ---------------- 缓存读写（约束缓存 + 规则版本必须同一条落库） ----------------
    @staticmethod
    def _cache_key(group: str, fingerprint: str) -> str:
        digest = hashlib.sha1(f"{group}:{fingerprint}".encode("utf-8")).hexdigest()[:10]
        return f"FC-{digest}"

    def _find_cache(self, group: str, fingerprint: str) -> dict[str, Any] | None:
        for row in store.rows(rules.CACHE_MODULE):
            if (
                row.get("因子组") == group
                and row.get("因子摘要") == fingerprint
                and not row.get("失效标记")
            ):
                return row
        return None

    def _write_cache(
        self,
        *,
        batch_no: str,
        group: str,
        factors: list[dict[str, Any]],
        rule_version: str,
        config_version: int,
        validations: dict[str, tuple[str, str]],
        force: bool = False,
    ) -> dict[str, Any]:
        fingerprint = rules.group_fingerprint(factors)
        existing = self._find_cache(group, fingerprint)
        if existing is not None and not force and existing.get("规则版本") == rule_version:
            # 摘要和规则版本都没变：直接复用旧缓存，缓存批次保持首次生成的那个批次号。
            existing["最近使用批次"] = batch_no
            existing["命中次数"] = int(existing.get("命中次数", 0)) + 1
            existing["复用"] = True
            return existing
        # 同组旧版本缓存打失效标记保留差异，但不物理删除。
        for row in store.rows(rules.CACHE_MODULE):
            if row.get("因子组") == group and row.get("因子摘要") != fingerprint:
                row["失效标记"] = True
        row = {
            "id": store.next_id(rules.CACHE_MODULE),
            "缓存批次": batch_no,
            "最近使用批次": batch_no,
            "缓存键": self._cache_key(group, fingerprint),
            "因子组": group,
            "因子摘要": fingerprint,
            "规则版本": rule_version,
            "配置版本": config_version,
            "因子数": len(factors),
            "校验通过数": sum(1 for result, _ in validations.values() if result.startswith("通过")),
            "校验结果": {code: result for code, (result, _) in validations.items()},
            "失效标记": False,
            "复用": False,
            "命中次数": 0,
            "生成时间": _now(),
        }
        store.rows(rules.CACHE_MODULE).append(row)
        return row

    def _invalidate_cache(self) -> int:
        """清空缓存不物理删除：打失效标记保留旧缓存差异，供重建后核对。"""
        count = 0
        for row in store.rows(rules.CACHE_MODULE):
            if not row.get("失效标记"):
                row["失效标记"] = True
                count += 1
        return count

    # ================================================================ 只读视图
    def preview(self) -> dict[str, Any]:
        """只读预览：复用同一套分组与校验，不落库、不写缓存。"""
        experiments, factors = self._load_snapshot()
        plan = self._plan_groups(factors, experiments)
        codes = set(self._experiment_index(experiments))
        groups = []
        for item in plan.values():
            cache = self._find_cache(item["group"], item["fingerprint"])
            groups.append({
                "因子组": item["group"],
                "因子数": len(item["factors"]),
                "实验数": len(item["experiment_codes"]),
                "因子摘要": item["fingerprint"],
                "规则版本": item["rule_version"],
                "缓存命中": cache is not None,
                "缓存批次": cache.get("缓存批次") if cache else "",
                "校验": [
                    {"因子编号": code, "结果": result, "摘要": summary}
                    for code, (result, summary) in item["validations"].items()
                ],
            })
        return {
            "分组": groups,
            "实验总数": len(experiments),
            "因子配置数": len(factors),
            "缺失实验": self._missing_experiments(plan, codes),
        }

    def detect_conflict(self) -> dict[str, Any]:
        """只读检测：在册批次与当前配置之间是否存在规则版本冲突。"""
        experiments, factors = self._load_snapshot()
        plan = self._plan_groups(factors, experiments)
        return self._conflict_report(self._active_batch(), plan)

    def _conflict_report(
        self, active: dict[str, Any] | None, plan: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if not active:
            return {"冲突": False, "变更因子组": [], "补算实验": [], "在册批次": ""}
        state = active.get("因子组状态") or {}
        changed: list[str] = []
        affected: list[str] = []
        for group, item in plan.items():
            recorded = state.get(group, {}).get("规则版本")
            if recorded and recorded != item["rule_version"]:
                changed.append(group)
                for code in item["experiment_codes"]:
                    if code not in affected:
                        affected.append(code)
        return {
            "冲突": bool(changed),
            "变更因子组": changed,
            "补算实验": affected,
            "在册批次": active.get("缓存批次"),
            "在册规则版本": active.get("规则版本"),
        }

    # ================================================================ 生成入口
    def generate(
        self,
        *,
        scope: str = "full",
        strategy: str = STRATEGY_REUSE,
        dry_run: bool = False,
        simulate_failure: str | None = None,
    ) -> dict[str, Any]:
        """整组生成因子约束分配表。

        ``scope``：full 整批生成 / incremental 只重算受影响因子组 / retry 只补未完成组。
        ``simulate_failure``：演练中断，处理到指定因子组时抛错，验证事务回滚与续算。
        """
        if scope not in {"full", "incremental", "retry"}:
            raise ValueError(f"不支持的生成范围：{scope}")
        if strategy not in {STRATEGY_REUSE, STRATEGY_REBUILD}:
            raise ValueError(f"不支持的复用策略：{strategy}")

        experiments, factors = self._load_snapshot()
        plan = self._plan_groups(factors, experiments)
        experiment_index = self._experiment_index(experiments)
        missing = self._missing_experiments(plan, set(experiment_index))
        active = self._active_batch()
        conflict = self._conflict_report(active, plan)

        if dry_run:
            return self._dry_run(plan, active, conflict, missing)
        if scope == "retry":
            # 续算面向最近一批（包括一组都还没完成的失败批次），不是只找「有效批次」。
            latest = self._latest_batch() or active
            return self._run_retry(latest, plan, experiment_index, missing, simulate_failure)
        return self._run(
            scope, strategy, plan, experiment_index, missing, active, conflict, simulate_failure
        )

    def _dry_run(
        self,
        plan: dict[str, dict[str, Any]],
        active: dict[str, Any] | None,
        conflict: dict[str, Any],
        missing: list[str],
    ) -> dict[str, Any]:
        state = (active or {}).get("因子组状态") or {}
        groups = []
        for group, item in plan.items():
            recorded = state.get(group, {})
            keep = (
                recorded.get("规则版本") == item["rule_version"]
                and recorded.get("状态") == "已完成"
            )
            groups.append({
                "因子组": group,
                "规则版本": item["rule_version"],
                "在册版本": recorded.get("规则版本", ""),
                "处理": "保持" if keep else "重算",
                "实验数": len(item["experiment_codes"]),
            })
        return {
            "预览": True,
            "冲突": conflict,
            "分组": groups,
            "缺失实验": missing,
        }

    # ---------------------------------------------------------------- 生成主体
    def _run(
        self,
        scope: str,
        strategy: str,
        plan: dict[str, dict[str, Any]],
        experiment_index: dict[str, dict[str, Any]],
        missing: list[str],
        active: dict[str, Any] | None,
        conflict: dict[str, Any],
        simulate_failure: str | None,
    ) -> dict[str, Any]:
        rebuild = strategy == STRATEGY_REBUILD
        if scope == "incremental" and active is None:
            scope = "full"
        if scope == "incremental" and rebuild:
            # 重建语义就是整批，不允许只换一部分组，否则旧缓存差异无法对齐。
            scope = "full"

        keeps_existing = scope != "full" and not rebuild and active is not None
        previous_state = self._groups_state(active) if keeps_existing else {}
        batch_no = str(active.get("缓存批次")) if keeps_existing else self._new_batch_no()
        # 只有沿用旧明细时才算「复用旧缓存并补算」；整批重生成不标补算。
        effective_conflict = conflict if keeps_existing else {"冲突": False, "补算实验": []}

        groups_to_run: list[str] = []
        for group, item in plan.items():
            recorded = previous_state.get(group, {})
            if keeps_existing and recorded.get("状态") == "已完成" and recorded.get("规则版本") == item["rule_version"]:
                continue
            groups_to_run.append(group)
        # 因子改层级后旧组可能整组消失：只有增量沿用旧明细时才需要清掉它的残留。
        dropped_groups = [
            group for group in previous_state if group not in plan
        ] if keeps_existing else []

        archived_batch = ""
        # 先单独提交批次头与留档：后续每个因子组各自提交，中断时只回滚当前组。
        with store.transaction(*TRANSACTION_MODULES):
            if rebuild and active is not None:
                archived = self._archive(active, reason=STRATEGY_LABELS[STRATEGY_REBUILD])
                archived_batch = archived["归档批次"]
                # 旧批次的明细已留在归档里，活动分配表只保留重建后的版本。
                self._purge_batch_details(archived_batch)
            if scope == "full" and active is not None and not rebuild:
                # 整批重生成：旧批次留档后清出活动分配表，避免两代明细并存。
                archived = self._archive(active, reason="整批重新生成", keep_status=True)
                self._purge_batch_details(archived["归档批次"])
            if rebuild:
                self._invalidate_cache()
            batch = self._open_batch(batch_no, scope, strategy, plan, conflict)
        store.bump_version(rules.BATCH_MODULE)

        interrupted = ""
        for group in groups_to_run:
            try:
                self._commit_group(
                    batch_no=batch_no,
                    group=group,
                    plan=plan,
                    experiment_index=experiment_index,
                    strategy=strategy,
                    conflict=effective_conflict,
                    simulate_failure=simulate_failure,
                )
            except RuntimeError as error:
                interrupted = str(error)
                self._mark_group_failed(batch_no, plan, group, interrupted, remaining=groups_to_run)
                break

        with store.transaction(*TRANSACTION_MODULES):
            for group in dropped_groups:
                self._purge_group_details(batch_no, group)
            self._finish_batch(
                self._batch_by_no(batch_no) or batch,
                plan=plan,
                missing=missing,
                conflict=effective_conflict,
                strategy=strategy,
            )
        store.bump_version(rules.BATCH_MODULE)

        batch = self._batch_by_no(batch_no) or batch
        return {
            "批次": self._batch_payload(batch),
            "本轮重算因子组": groups_to_run,
            "保持因子组": [g for g in plan if g not in groups_to_run],
            "缺失实验": missing,
            "补算实验": batch.get("补算实验", []),
            "留档": archived_batch,
            "中断": interrupted,
        }

    def _run_retry(
        self,
        active: dict[str, Any] | None,
        plan: dict[str, dict[str, Any]],
        experiment_index: dict[str, dict[str, Any]],
        missing: list[str],
        simulate_failure: str | None,
    ) -> dict[str, Any]:
        """中断后续算：只补未完成组，已完成组的明细和版本保持不变。"""
        if active is None:
            return self._run(
                "full", STRATEGY_REUSE, plan, experiment_index, missing, None,
                {"冲突": False, "补算实验": []}, simulate_failure,
            )
        state = self._groups_state(active)
        pending = [
            group for group in plan
            if state.get(group, {}).get("状态") != "已完成"
        ]
        batch_no = str(active.get("缓存批次"))

        with store.transaction(*TRANSACTION_MODULES):
            retry_batch = self._batch_by_no(batch_no)
            retry_batch["状态"] = BATCH_RUNNING
            retry_batch["重试次数"] = int(retry_batch.get("重试次数", 0)) + 1
            retry_batch["更新时间"] = _now()
        store.bump_version(rules.BATCH_MODULE)

        interrupted = ""
        for group in pending:
            try:
                self._commit_group(
                    batch_no=batch_no,
                    group=group,
                    plan=plan,
                    experiment_index=experiment_index,
                    strategy=STRATEGY_REUSE,
                    conflict={"冲突": False, "补算实验": []},
                    simulate_failure=simulate_failure,
                )
            except RuntimeError as error:
                interrupted = str(error)
                self._mark_group_failed(batch_no, plan, group, interrupted, remaining=pending)
                break

        with store.transaction(*TRANSACTION_MODULES):
            self._finish_batch(
                self._batch_by_no(batch_no) or retry_batch,
                plan=plan,
                missing=missing,
                conflict={"冲突": False, "补算实验": []},
                strategy=STRATEGY_REUSE,
                retry=True,
            )
        store.bump_version(rules.BATCH_MODULE)

        batch = self._batch_by_no(batch_no) or retry_batch
        return {
            "批次": self._batch_payload(batch),
            "本轮重算因子组": pending,
            "已完成保持": [
                group for group, item in (batch.get("因子组状态") or {}).items()
                if item.get("状态") == "已完成"
            ],
            "缺失实验": missing,
            "补算实验": batch.get("补算实验", []),
            "中断": interrupted,
        }

    def _commit_group(
        self,
        *,
        batch_no: str,
        group: str,
        plan: dict[str, dict[str, Any]],
        experiment_index: dict[str, dict[str, Any]],
        strategy: str,
        conflict: dict[str, Any],
        simulate_failure: str | None = None,
    ) -> None:
        """单个因子组一个事务：明细、缓存、规则版本、批次状态一起落库。"""
        item = plan[group]
        with store.transaction(*TRANSACTION_MODULES):
            details = self._build_group_details(
                item=item,
                experiment_index=experiment_index,
                fallback_batch=batch_no,
                mark_compensated=(strategy == STRATEGY_REUSE and bool(conflict.get("冲突"))),
            )
            self._commit_group_details(batch_no, group, details)
            self._write_cache(
                batch_no=batch_no,
                group=group,
                factors=item["factors"],
                rule_version=item["rule_version"],
                config_version=item["config_version"],
                validations=item["validations"],
                force=strategy == STRATEGY_REBUILD,
            )
            batch = self._batch_by_no(batch_no)
            state = self._groups_state(batch)
            state[group] = {
                "状态": "已完成",
                "规则版本": item["rule_version"],
                "因子摘要": item["fingerprint"],
                "明细数": len(details),
            }
            batch["因子组状态"] = state
            batch["更新时间"] = _now()
            if simulate_failure == group:
                # 演练中断：本组事务整体回滚（明细、缓存、版本一起撤销），
                # 之前已提交的组不受影响，重试时只补未完成组。
                raise RuntimeError(f"因子组「{group}」处理中断")
        store.bump_version(rules.ALLOCATION_MODULE)

    def _build_group_details(
        self,
        *,
        item: dict[str, Any],
        experiment_index: dict[str, dict[str, Any]],
        fallback_batch: str,
        mark_compensated: bool,
    ) -> list[dict[str, Any]]:
        """同一组内所有实验复用同一份已校验约束，逐条产出回执明细。"""
        cache = self._find_cache(item["group"], item["fingerprint"])
        cache_batch = str(cache.get("缓存批次")) if cache else fallback_batch
        cache_key = str(cache.get("缓存键")) if cache else self._cache_key(item["group"], item["fingerprint"])
        details: list[dict[str, Any]] = []
        for factor in item["factors"]:
            factor_code = str(factor.get("因子编号") or factor.get("id"))
            validation, summary = item["validations"][factor_code]
            for code in rules.experiment_codes(factor):
                experiment = experiment_index.get(code)
                if experiment is None:
                    continue
                snapshot = rules.allocation_snapshot(experiment, factor)
                details.append({
                    "id": 0,
                    "分配批次": fallback_batch,
                    "实验编号": code,
                    "实验名称": experiment.get("实验名称", ""),
                    "因子编号": factor_code,
                    "因子名称": factor.get("因子名称", ""),
                    "因子层级": item["group"],
                    "因子组": item["group"],
                    "取值": snapshot["取值"],
                    "分配比例": snapshot["分配比例"],
                    "对照比例": snapshot["对照比例"],
                    "实验比例": snapshot["实验比例"],
                    "对照样本": snapshot["对照样本"],
                    "实验样本": snapshot["实验样本"],
                    "约束校验": validation,
                    "约束摘要": summary,
                    "缓存批次": cache_batch,
                    "缓存键": cache_key,
                    "规则版本": item["rule_version"],
                    "因子摘要": snapshot["因子摘要"],
                    "生成状态": RECEIPT_READY if validation.startswith("通过") else RECEIPT_REVIEW,
                    "补算标记": "补算" if mark_compensated else "无",
                })
        return details

    def _commit_group_details(
        self, batch_no: str, group: str, details: list[dict[str, Any]]
    ) -> None:
        """整组替换：替换本组在本批次的明细，其他组的明细原样保留。

        新批次（full/rebuild）打开时已清掉活动分配表，因此这里只需按组替换；
        增量与续算沿用同一批次号，只会覆盖本轮重算的组。
        """
        rows = store.rows(rules.ALLOCATION_MODULE)
        previous = {
            (row.get("实验编号"), row.get("因子编号"), row.get("因子组")): int(row.get("id", 0))
            for row in rows
            if row.get("分配批次") == batch_no
        }
        kept = [
            row for row in rows
            if not (row.get("分配批次") == batch_no and row.get("因子组") == group)
        ]
        next_id = max((int(row.get("id", 0)) for row in kept), default=0) + 1
        committed: list[dict[str, Any]] = []
        for detail in details:
            row = dict(detail)
            key = (row.get("实验编号"), row.get("因子编号"), row.get("因子组"))
            # 同一批次里同一条分配沿用原 id，页面与详情链接在重算后依旧稳定。
            row["id"] = previous.get(key) or next_id
            if not previous.get(key):
                next_id += 1
            committed.append(row)
        rows[:] = kept + committed

    # --------------------------------------------------------------- 批次落库
    def _new_batch_no(self) -> str:
        seq = _next_value(rules.BATCH_MODULE, "批次序号")
        return f"FB-{datetime.now().strftime('%Y%m%d')}-{seq:03d}"

    @staticmethod
    def _groups_state(batch: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        return copy.deepcopy((batch or {}).get("因子组状态") or {})

    def _open_batch(
        self,
        batch_no: str,
        scope: str,
        strategy: str,
        plan: dict[str, dict[str, Any]],
        conflict: dict[str, Any],
    ) -> dict[str, Any]:
        existing = self._batch_by_no(batch_no)
        if existing is not None:
            existing["状态"] = BATCH_RUNNING
            existing["复用策略"] = strategy
            existing["更新时间"] = _now()
            return existing
        batch = {
            "id": store.next_id(rules.BATCH_MODULE),
            "批次序号": _next_value(rules.BATCH_MODULE, "批次序号"),
            "缓存批次": batch_no,
            "状态": BATCH_RUNNING,
            "范围": scope,
            "复用策略": strategy,
            "规则版本": rules.combined_rule_version(
                item["rule_version"] for item in plan.values()
            ),
            "因子组状态": {},
            "补算实验": list(conflict.get("补算实验", [])) if scope in {"incremental", "retry"} else [],
            "缺失实验": [],
            "重试次数": 0,
            "生成时间": _now(),
            "更新时间": _now(),
        }
        store.rows(rules.BATCH_MODULE).append(batch)
        return batch

    def _finish_batch(
        self,
        batch: dict[str, Any],
        *,
        plan: dict[str, dict[str, Any]],
        missing: list[str],
        conflict: dict[str, Any],
        strategy: str,
        retry: bool = False,
    ) -> None:
        state = {
            group: item for group, item in self._groups_state(batch).items()
            if group in plan
        }
        for group, item in plan.items():
            if group not in state:
                state[group] = {
                    "状态": "未完成",
                    "规则版本": item["rule_version"],
                    "因子摘要": item["fingerprint"],
                    "明细数": 0,
                }
        batch["因子组状态"] = state
        batch["缺失实验"] = missing
        batch["更新时间"] = _now()
        if retry:
            batch["重试次数"] = max(int(batch.get("重试次数", 0)), 1)
        if any(item.get("状态") != "已完成" for item in state.values()):
            batch["状态"] = BATCH_PARTIAL if any(
                item.get("状态") == "已完成" for item in state.values()
            ) else BATCH_FAILED
        else:
            batch["状态"] = BATCH_DONE
        if strategy == STRATEGY_REUSE and conflict.get("补算实验"):
            batch["补算实验"] = list(conflict["补算实验"])
        elif strategy == STRATEGY_REBUILD:
            batch["补算实验"] = []

    def _mark_group_failed(
        self,
        batch_no: str,
        plan: dict[str, dict[str, Any]],
        group: str,
        reason: str,
        remaining: Iterable[str],
    ) -> None:
        """当前组事务已回滚：只更新批次头，其他已完成组不受影响。"""
        batch = self._batch_by_no(batch_no)
        if batch is None:
            return
        state = self._groups_state(batch)
        for name in {group, *remaining}:
            item = plan.get(name)
            if item is None:
                continue
            current = state.get(name, {})
            if current.get("状态") == "已完成":
                continue
            state[name] = {
                "状态": "未完成",
                "规则版本": item["rule_version"],
                "因子摘要": item["fingerprint"],
                "明细数": current.get("明细数", 0),
                "失败原因": reason,
            }
        batch["因子组状态"] = state
        batch["状态"] = BATCH_FAILED
        batch["更新时间"] = _now()

    @staticmethod
    def _purge_group_details(batch_no: str, group: str) -> None:
        rows = store.rows(rules.ALLOCATION_MODULE)
        rows[:] = [
            row for row in rows
            if not (row.get("分配批次") == batch_no and row.get("因子组") == group)
        ]

    @staticmethod
    def _purge_batch_details(batch_no: str) -> None:
        rows = store.rows(rules.ALLOCATION_MODULE)
        rows[:] = [row for row in rows if row.get("分配批次") != batch_no]

    def _archive(
        self, batch: dict[str, Any], *, reason: str, keep_status: bool = False
    ) -> dict[str, Any]:
        """整批重建前留档：旧批次明细与旧缓存差异一起保存，便于核对回退范围。

        ``keep_status`` 用于整批重新生成：旧批次只是被替换，保留其完成状态便于追溯；
        重建路径则标记为「已归档」，与活动批次区分。
        """
        batch_no = str(batch.get("缓存批次", ""))
        archive = {
            "id": store.next_id(rules.ARCHIVE_MODULE),
            "归档批次": batch_no,
            "原因": reason,
            "状态": batch.get("状态"),
            "规则版本": batch.get("规则版本"),
            "明细": copy.deepcopy(self._batch_details(batch_no)),
            "缓存差异": [
                copy.deepcopy(row) for row in store.rows(rules.CACHE_MODULE)
                if row.get("缓存批次") == batch_no
            ],
            "归档时间": _now(),
        }
        store.rows(rules.ARCHIVE_MODULE).append(archive)
        if not keep_status:
            batch["状态"] = BATCH_ARCHIVED
        batch["更新时间"] = _now()
        return archive

    # ================================================================ 事务残留核对
    def verify(self) -> dict[str, Any]:
        """核对分配表与约束缓存是否一一对齐，确认没有半套事务残留。

        检查项：
        * 明细引用的缓存批次/缓存键/规则版本能对应到一条有效缓存；
        * 明细的缓存批次与缓存记录的批次一致，同一组同版本不并行两套批次；
        * 批次头里「已完成」的组一定有明细，未完成的组不能有明细；
        * 明细总数与批次头记录的组明细数一致。
        """
        issues: list[str] = []
        cache_by_key = {
            (row.get("缓存键"), row.get("规则版本")): row
            for row in store.rows(rules.CACHE_MODULE)
            if not row.get("失效标记")
        }
        layer_versions: dict[tuple[str, str], set[str]] = {}
        detail_counts: dict[tuple[str, str], int] = {}
        for row in store.rows(rules.ALLOCATION_MODULE):
            key = (row.get("缓存键"), row.get("规则版本"))
            if key not in cache_by_key:
                issues.append(
                    f"明细 {row.get('实验编号')}/{row.get('因子编号')} 引用的缓存 {row.get('缓存键')} "
                    f"版本 {row.get('规则版本')} 不存在或已失效"
                )
            counts_key = (str(row.get("分配批次")), str(row.get("因子组")))
            detail_counts[counts_key] = detail_counts.get(counts_key, 0) + 1
            if key in cache_by_key:
                cache = cache_by_key[key]
                if cache.get("缓存批次") != row.get("缓存批次"):
                    issues.append(
                        f"明细 {row.get('实验编号')}/{row.get('因子编号')} 的缓存批次 "
                        f"{row.get('缓存批次')} 与缓存记录 {cache.get('缓存批次')} 不一致"
                    )
                layer_versions.setdefault((str(row.get("因子组")), key[1]), set()).add(
                    str(row.get("缓存批次"))
                )

        # 同一因子组在同一规则版本下只能挂在一个缓存批次上，否则就是两套规则并行。
        for (group, version), batches in layer_versions.items():
            if len(batches) > 1:
                issues.append(
                    f"因子组「{group}」在规则版本 {version} 下出现多个缓存批次："
                    f"{'、'.join(sorted(batches))}"
                )

        active_no = str((self._active_batch() or {}).get("缓存批次"))
        for batch in store.rows(rules.BATCH_MODULE):
            if batch.get("状态") == BATCH_ARCHIVED:
                # 已归档批次的明细留在留档表里，不参与活动批次核对。
                continue
            batch_no = str(batch.get("缓存批次"))
            if batch_no != active_no:
                # 被新批次替换的历史批次也不再参与活动分配表核对。
                continue
            state = batch.get("因子组状态") or {}
            for group, item in state.items():
                count = detail_counts.get((batch_no, group), 0)
                if item.get("状态") == "已完成" and count == 0:
                    issues.append(f"批次 {batch_no} 因子组「{group}」标记已完成但没有明细")
                if item.get("状态") != "已完成" and count > 0:
                    issues.append(f"批次 {batch_no} 因子组「{group}」未完成却有 {count} 条明细残留")
                if item.get("状态") == "已完成" and int(item.get("明细数", 0)) != count:
                    issues.append(
                        f"批次 {batch_no} 因子组「{group}」明细数不一致："
                        f"批次头 {item.get('明细数')} / 实际 {count}"
                    )
        return {"ok": not issues, "问题": issues, "检查项": 5}

    # ================================================================ 单因子改动
    def update_factor(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """改一条因子配置并只重算受影响的因子组。

        返回受影响组、复用的缓存批次和重算结果；未受影响的组保持原有明细和规则版本。
        """
        entry = store.find(rules.CONFIG_MODULE, entry_id)
        if entry is None:
            raise KeyError(entry_id)
        before = rules.factor_fingerprint(entry)
        for field, value in values.items():
            if field in rules.FACTOR_FIELDS:
                entry[field] = value
        after = rules.factor_fingerprint(entry)
        store.bump_version(rules.CONFIG_MODULE)

        if before == after:
            # 配置没变就不触发重算，已完成的分配和缓存批次保持原样。
            return {
                "因子配置": entry,
                "变更": False,
                "受影响因子组": [],
                "重算结果": None,
            }
        if self._active_batch() is None:
            # 还没有分配表时不顺手生成，避免改一条配置就隐式建整批。
            return {
                "因子配置": entry,
                "变更": True,
                "受影响因子组": [],
                "缓存批次": "",
                "重算结果": None,
            }
        result = self.generate(scope="incremental", strategy=STRATEGY_REUSE)
        return {
            "因子配置": entry,
            "变更": True,
            "受影响因子组": result.get("本轮重算因子组", []),
            "保持因子组": result.get("保持因子组", []),
            "缓存批次": (result.get("批次") or {}).get("缓存批次"),
            "重算结果": result,
        }

    # ================================================================ 查询接口
    def list_allocations(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        state: str | None = None,
        page: int = 1,
        size: int = 20,
        batch_no: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        target = batch_no or (self._active_batch() or {}).get("缓存批次")
        rows = [
            row for row in store.rows(rules.ALLOCATION_MODULE)
            if not target or row.get("分配批次") == target
        ]
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [
                row for row in rows
                if keyword.casefold() in str(row.get("实验编号", "")).casefold()
                or keyword.casefold() in str(row.get("因子编号", "")).casefold()
            ]
        if state:
            rows = [row for row in rows if row.get("生成状态") == state]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_allocation(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(rules.ALLOCATION_MODULE, entry_id)
        if row is None:
            return None
        payload = dict(row)
        batch = self._batch_by_no(str(row.get("分配批次", "")))
        payload["批次"] = self._batch_payload(batch) if batch else None
        payload["缓存"] = next(
            (
                dict(cache) for cache in store.rows(rules.CACHE_MODULE)
                if cache.get("缓存键") == row.get("缓存键") and not cache.get("失效标记")
            ),
            None,
        )
        return payload

    def list_batches(self) -> list[dict[str, Any]]:
        return [
            self._batch_payload(batch)
            for batch in sorted(
                store.rows(rules.BATCH_MODULE),
                key=lambda r: int(r.get("id", 0)),
                reverse=True,
            )
        ]

    def list_cache(self) -> dict[str, Any]:
        rows = [
            dict(row) for row in sorted(
                store.rows(rules.CACHE_MODULE), key=lambda r: int(r.get("id", 0)), reverse=True)
        ]
        return {"items": rows, "total": len(rows)}

    def list_archive(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in sorted(
                store.rows(rules.ARCHIVE_MODULE), key=lambda r: int(r.get("id", 0)), reverse=True)
        ]

    def consumer_view(self, batch_no: str | None = None) -> dict[str, Any]:
        """指标定义（V015）消费链路：读取实验编号、缓存批次与因子层。"""
        target = batch_no or (self._active_batch() or {}).get("缓存批次")
        rows = [row for row in store.rows(rules.ALLOCATION_MODULE) if row.get("分配批次") == target]
        items = [
            {
                "实验编号": row.get("实验编号"),
                "分配批次": row.get("分配批次"),
                "缓存批次": row.get("缓存批次"),
                "因子层级": row.get("因子层级"),
                "因子编号": row.get("因子编号"),
                "规则版本": row.get("规则版本"),
                "约束校验": row.get("约束校验"),
                "生成状态": row.get("生成状态"),
            }
            for row in rows
        ]
        batch = self._batch_by_no(target) if target else None
        return {
            "缓存批次": target,
            "规则版本": (batch or {}).get("规则版本", ""),
            "明细": items,
            "因子层": sorted({item["因子层级"] for item in items}),
            "覆盖实验": sorted({item["实验编号"] for item in items}),
        }
