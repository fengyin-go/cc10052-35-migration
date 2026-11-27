<template>
  <section class="page" data-module="archive_migration">
    <header class="page-head">
      <div>
        <h2>实验归档迁移与恢复</h2>
        <p class="page-desc">
          进程内存归档整包迁移到 SQLite：按原归档号查迁移进度与缺失材料，原接口继续领取恢复，
          切片只成功一次，重启或回滚后仍可复核归档版次、样本版次与证据。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="bootstrap" type="button" @click="onBootstrap">
          载入 A17 演练现场
        </button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="loadAll">
      <label class="filter-item">
        <span>原归档号</span>
        <input v-model="archiveNo" data-testid="archive-no" placeholder="如 A17" required />
      </label>
      <button class="btn primary" data-testid="query-archive" type="submit">查询迁移进度</button>
      <button class="btn" type="button" data-testid="migrate" @click="onMigrate">整包迁移</button>
      <button class="btn" type="button" data-testid="submit-restore" @click="onSubmitRestore">
        按原编号受理恢复
      </button>
    </form>

    <p v-if="message" class="status-legend" data-testid="message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>

    <!-- 迁移进度与缺失材料 -->
    <h3 class="section-title">迁移进度（按旧编号，不合并版次）</h3>
    <table class="data-table" data-testid="progress-table">
      <thead>
        <tr>
          <th>原归档号</th><th>归档版次</th><th>阶段</th><th>整包状态</th>
          <th>阶段进度</th><th>缺失材料</th><th>恢复凭证</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="ver in progress?.versions ?? []" :key="`${ver.archiveNo}-${ver.version}`"
            :data-testid="`progress-row-${ver.version}`" :data-status="ver.packageStatus">
          <td>{{ ver.archiveNo }}</td>
          <td>{{ ver.version }}</td>
          <td>{{ ver.stage }}</td>
          <td>{{ ver.packageStatus }}</td>
          <td>{{ ver.finishedStages }}/{{ ver.totalStages }}</td>
          <td>
            <span v-if="ver.missingFields.length" class="missing-tag">
              {{ ver.missingFields.join('、') }}
            </span>
            <span v-else>—</span>
          </td>
          <td>
            <span v-if="progress?.credential" class="credential-tag">已签发</span>
            <span v-else>—</span>
          </td>
        </tr>
        <tr v-if="!progress?.versions?.length">
          <td colspan="7" class="empty-state">输入原归档号查询迁移进度</td>
        </tr>
      </tbody>
    </table>

    <!-- 恢复请求 -->
    <h3 class="section-title">恢复请求（已受理可从原请求继续，已办结不重复执行）</h3>
    <table class="data-table" data-testid="restore-table">
      <thead>
        <tr>
          <th>请求编号</th><th>原归档号</th><th>版次</th><th>所处阶段</th><th>状态</th>
          <th>实际数据源</th><th>样本版次</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="req in restores" :key="req.requestNo" :data-testid="`restore-row-${req.requestNo}`"
            :data-status="req.requestStatus">
          <td>{{ req.requestNo }}</td>
          <td>{{ req.archiveNo }}</td>
          <td>{{ req.version }}</td>
          <td>{{ req.stage }}</td>
          <td>{{ req.requestStatus }}</td>
          <td>{{ req.dataSource ?? '—' }}</td>
          <td>{{ req.sampleVersion ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`continue-${req.requestNo}`"
                    :disabled="req.requestStatus === '已办结'" @click="onContinue(req.requestNo)">
              继续办理
            </button>
            <button class="link" type="button" :data-testid="`detail-${req.requestNo}`" @click="onDetail(req.requestNo)">
              复核证据
            </button>
          </td>
        </tr>
        <tr v-if="!restores.length">
          <td colspan="8" class="empty-state">暂无恢复请求</td>
        </tr>
      </tbody>
    </table>

    <!-- 选中请求的阶段流水与切片证据 -->
    <template v-if="selected">
      <h3 class="section-title">请求 {{ selected.requestNo }} 的阶段流水与切片证据</h3>
      <table class="data-table" data-testid="event-table">
        <thead><tr><th>事件</th><th>从阶段</th><th>到阶段</th><th>数据源</th><th>说明</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="(ev, idx) in selected.events ?? []" :key="idx" :data-testid="`event-${idx}`">
            <td>{{ ev.eventType }}</td><td>{{ ev.fromStage ?? '—' }}</td><td>{{ ev.toStage ?? '—' }}</td>
            <td>{{ ev.dataSource ?? '—' }}</td><td>{{ ev.note }}</td><td>{{ ev.recordedAt }}</td>
          </tr>
        </tbody>
      </table>

      <article v-if="selected.slice" class="evidence-card" data-testid="evidence-card">
        <h4>切片明细（可核对原归档号与样本版次）</h4>
        <ul class="evidence-list">
          <li>切片批次：{{ selected.slice.sliceBatch }}</li>
          <li>原归档号：{{ selected.slice.archiveNo }} ｜ 归档版次：{{ selected.slice.version }}</li>
          <li>归档存储版本：{{ selected.slice.storageVersion }} ｜ 样本版次：{{ selected.slice.sampleVersion }}</li>
          <li>实际数据源：{{ selected.slice.dataSource }}</li>
          <li>实际计算输入：<code>{{ JSON.stringify(selected.slice.computeInput) }}</code></li>
          <li>指标结果：{{ selected.slice.metricValue }}</li>
          <li>证据指纹：{{ String(selected.slice.evidence.inputHash ?? '') }}</li>
          <li>原摘要复核：{{ String(selected.slice.evidence.summarySnapshot ?? '') }}</li>
        </ul>
      </article>
    </template>

    <!-- 部署/回滚流水 -->
    <h3 class="section-title">上线 / 回滚流水（重启后仍可说明处理结果）</h3>
    <table class="data-table" data-testid="deployment-table">
      <thead><tr><th>部署编号</th><th>动作</th><th>结果</th><th>说明</th><th>时间</th></tr></thead>
      <tbody>
        <tr v-for="dep in deployments" :key="dep.deploymentNo + dep.recordedAt"
            :data-testid="`deployment-${dep.action}`">
          <td>{{ dep.deploymentNo }}</td><td>{{ dep.action }}</td><td>{{ dep.result }}</td>
          <td>{{ dep.note }}</td><td>{{ dep.recordedAt }}</td>
        </tr>
        <tr v-if="!deployments.length">
          <td colspan="5" class="empty-state">暂无部署记录</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  bootstrapScenario,
  continueRestore,
  fetchDeployments,
  fetchProgress,
  fetchRestore,
  fetchRestores,
  fetchSlices,
  migratePackage,
  submitRestore,
  type MigrationProgress,
  type RestoreRequest,
  type DeploymentRecord,
} from '@/api/archive-migration'

const archiveNo = ref('A17')
const progress = ref<MigrationProgress | null>(null)
const restores = ref<RestoreRequest[]>([])
const deployments = ref<DeploymentRecord[]>([])
const selected = ref<RestoreRequest | null>(null)
const message = ref('')
const errorMessage = ref('')

async function loadAll() {
  errorMessage.value = ''
  try {
    const no = archiveNo.value.trim()
    if (no) {
      progress.value = await fetchProgress(no)
      restores.value = await fetchRestores(no)
    } else {
      restores.value = await fetchRestores()
    }
    deployments.value = await fetchDeployments()
    await fetchSlices(no || undefined)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询失败'
  }
}

async function onMigrate() {
  errorMessage.value = ''
  try {
    const result = await migratePackage(archiveNo.value.trim())
    message.value = (result as { message?: string }).message ?? '整包迁移已执行'
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移失败'
  }
}

async function onSubmitRestore() {
  errorMessage.value = ''
  try {
    const result = await submitRestore(
      archiveNo.value.trim(),
      `idem-${archiveNo.value.trim()}-${Date.now()}`,
    )
    message.value = (result as { message?: string }).message ?? '恢复请求已受理'
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '受理失败'
  }
}

async function onContinue(requestNo: string) {
  errorMessage.value = ''
  try {
    const result = await continueRestore(requestNo)
    message.value = (result as { message?: string }).message ?? '继续办理完成'
    await loadAll()
    await onDetail(requestNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '继续办理失败'
  }
}

async function onDetail(requestNo: string) {
  errorMessage.value = ''
  try {
    selected.value = await fetchRestore(requestNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核失败'
  }
}

async function onBootstrap() {
  errorMessage.value = ''
  try {
    const result = await bootstrapScenario()
    message.value = (result as { message?: string }).message ?? '演练现场已载入'
    archiveNo.value = 'A17'
    selected.value = null
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '现场载入失败'
  }
}

onMounted(loadAll)
</script>

<style scoped>
.section-title {
  margin: 20px 0 8px;
  font-size: 15px;
}
.missing-tag {
  color: #b45309;
  font-size: 12px;
}
.credential-tag {
  color: #1d4ed8;
  font-size: 12px;
}
.evidence-card {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px 16px;
  margin: 12px 0;
  background: #f8fafc;
}
.evidence-list {
  margin: 8px 0 0;
  padding-left: 18px;
  display: grid;
  gap: 6px;
}
.evidence-list code {
  background: #eef2ff;
  padding: 1px 6px;
  border-radius: 4px;
}
</style>
