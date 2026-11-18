<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">多维分析实验平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。</span>
        <span class="head-user">当前值班：{{ store.operator }} · {{ store.shiftLabel }}</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()

const navItems = [{ label: "运营概览", path: "/" }, { label: "实验设计", path: "/experiment" }, { label: "样本分群", path: "/sample_group" }, { label: "指标定义", path: "/metric_def" }, { label: "AB结果分析", path: "/ab_result" }, { label: "分群对比", path: "/segment_compare" }, { label: "多维下钻", path: "/drill_down" }, { label: "数据筛选", path: "/data_filter" }, { label: "归因分析", path: "/attr_analysis" }, { label: "异常检测", path: "/anomaly_detect" }, { label: "预测模型", path: "/forecast_model" }, { label: "数据导出", path: "/data_export" }, { label: "同期群追踪", path: "/cohort_track" }, { label: "可视化配置", path: "/chart_config" }, { label: "洞察报告", path: "/insight_report" }, { label: "数据新鲜度", path: "/data_freshness" }, { label: "口径注册", path: "/metric_alias" }, { label: "实验因子", path: "/expr_factor" }, { label: "评估看板", path: "/evaluate_board" }, { label: "因子约束分配", path: "/factor_allocation" }]
</script>
