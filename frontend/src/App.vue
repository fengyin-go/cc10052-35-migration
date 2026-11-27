<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">实验指标分析与归因评估平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">支撑AB实验分组、指标口径计算、显著性检验、归因分析与报告分发的实验评估平台</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [{ label: "运营概览", path: "/" }, { label: "归档迁移与恢复", path: "/archive-migration" }, { label: "事件接入", path: "/event_stream" }, { label: "实验设计", path: "/exp_design" }, { label: "分组分配器", path: "/variant_assign" }, { label: "指标口径中心", path: "/metric_catalog" }, { label: "指标计算作业", path: "/metric_compute" }, { label: "显著性检验", path: "/significance_test" }, { label: "归因分析器", path: "/attr_analysis" }, { label: "人群圈选器", path: "/cohort_builder" }, { label: "报告生成器", path: "/report_gen" }, { label: "报告分发", path: "/report_dist" }, { label: "窗口调度器", path: "/window_scheduler" }, { label: "数据时效监控", path: "/data_freshness" }, { label: "指标异常检测", path: "/anomaly_metric" }, { label: "实验归档库", path: "/exp_archive" }, { label: "样本量核对", path: "/sample_ratio" }, { label: "AA预检器", path: "/aa_check" }, { label: "切片分析器", path: "/segment_slice" }, { label: "曝光日志接入", path: "/exposure_log" }, { label: "结论评审队列", path: "/conclusion_review" }, { label: "指标查询服务", path: "/metric_api" }]
</script>
