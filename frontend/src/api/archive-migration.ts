/** 归档迁移与恢复接口：按原归档号查迁移进度、受理/续办恢复、核对切片证据。 */
import { request } from './client'

export type MigrationVersion = {
  archiveNo: string
  version: string
  packageStatus: string
  stage: string
  summary?: string
  metrics?: Record<string, unknown>
  missingFields: string[]
  totalStages: number
  finishedStages: number
  journal: { stage: string; status: string; detail: Record<string, unknown>; recordedAt: string }[]
}

export type MigrationProgress = {
  archiveNo: string
  versions: MigrationVersion[]
  credential: string | null
}

export type RestoreRequest = {
  requestNo: string
  archiveNo: string
  version: string
  idempotencyKey: string | null
  stage: string
  requestStatus: string
  credentialValid: boolean
  dataSource: string | null
  storageVersion: string | null
  sampleVersion: string | null
  sliceBatch: string | null
  resultSummary: string | null
  idempotentHit: boolean
  createdAt: string
  updatedAt: string
  events?: {
    eventType: string
    fromStage: string | null
    toStage: string | null
    dataSource: string | null
    note: string
    recordedAt: string
  }[]
  slice?: SliceRecovery
}

export type SliceRecovery = {
  requestNo: string
  sliceBatch: string
  archiveNo: string
  version: string
  expCode: string
  storageVersion: string
  sampleVersion: string
  dataSource: string
  computeInput: Record<string, unknown>
  metricValue: number
  evidence: Record<string, unknown>
  recoverStatus: string
  createdAt: string
}

export type DeploymentRecord = {
  deploymentNo: string
  action: string
  result: string
  note: string
  recordedAt: string
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function fetchProgress(archiveNo: string): Promise<MigrationProgress> {
  const response = await request(`/api/archive-migration/packages/${encodeURIComponent(archiveNo)}/progress`)
  return asJson<MigrationProgress>(response)
}

export async function migratePackage(archiveNo: string, version?: string): Promise<unknown> {
  const response = await request('/api/archive-migration/packages/migrate', {
    method: 'POST',
    body: JSON.stringify({ archiveNo, version: version || null }),
  })
  return asJson(response)
}

export async function submitRestore(archiveNo: string, idempotencyKey?: string): Promise<unknown> {
  const response = await request('/api/archive-migration/restores', {
    method: 'POST',
    body: JSON.stringify({ archiveNo, idempotencyKey: idempotencyKey || null }),
  })
  return asJson(response)
}

export async function continueRestore(requestNo: string): Promise<unknown> {
  const response = await request('/api/archive-migration/restores/continue', {
    method: 'POST',
    body: JSON.stringify({ requestNo }),
  })
  return asJson(response)
}

export async function fetchRestore(requestNo: string): Promise<RestoreRequest> {
  const response = await request(`/api/archive-migration/restores/${encodeURIComponent(requestNo)}`)
  return asJson<RestoreRequest>(response)
}

export async function fetchRestores(archiveNo?: string): Promise<RestoreRequest[]> {
  const query = archiveNo ? `?archiveNo=${encodeURIComponent(archiveNo)}` : ''
  const response = await request(`/api/archive-migration/restores${query}`)
  const payload = await asJson<{ items: RestoreRequest[] }>(response)
  return payload.items
}

export async function fetchSlices(archiveNo?: string): Promise<SliceRecovery[]> {
  const query = archiveNo ? `?archiveNo=${encodeURIComponent(archiveNo)}` : ''
  const response = await request(`/api/archive-migration/slices${query}`)
  const payload = await asJson<{ items: SliceRecovery[] }>(response)
  return payload.items
}

export async function fetchDeployments(): Promise<DeploymentRecord[]> {
  const response = await request('/api/archive-migration/deployments')
  const payload = await asJson<{ items: DeploymentRecord[] }>(response)
  return payload.items
}

export async function bootstrapScenario(): Promise<unknown> {
  const response = await request('/api/archive-migration/scenario/bootstrap', { method: 'POST' })
  return asJson(response)
}
