import { expect, test } from '@playwright/test'

// 因子约束分配：整组生成、逐条回执、详情一致性、单因子增量重算。
// 后端为内存仓库，用例按顺序执行，避免互相覆盖批次。

type AllocationRow = {
  id: number
  分配批次: string
  实验编号: string
  因子编号: string
  缓存批次: string
  规则版本: string
}

test.describe.configure({ mode: 'serial' })

test.use({ baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173' })

async function generateFull(page: import('@playwright/test').Page) {
  const response = await page.request.post('/api/factor_allocation/generate', {
    data: { scope: 'full', strategy: 'reuse' },
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json())['批次'] as Record<string, any>
}

async function listRows(page: import('@playwright/test').Page): Promise<AllocationRow[]> {
  const response = await page.request.get('/api/factor_allocation?size=200')
  expect(response.ok()).toBeTruthy()
  return (await response.json()).items as AllocationRow[]
}

test('因子约束分配：整组生成、批次面板、列表与详情一致', async ({ page }) => {
  const browserErrors: string[] = []
  page.on('pageerror', (error) => browserErrors.push(error.message))
  page.on('response', (response) => {
    if (response.url().includes('/api/') && response.status() >= 400) {
      browserErrors.push(`${response.status()} ${response.url()}`)
    }
  })

  const batch = await generateFull(page)
  await page.goto('/factor_allocation')
  await expect(page.getByTestId('batch-no')).toHaveText(batch['缓存批次'])
  await expect(page.getByTestId('batch-status')).toHaveText('已完成')
  await expect(page.getByTestId('completed-groups')).toContainText('UI层')
  await expect(page.getByTestId('pending-groups')).toContainText('—')

  const rows = page.locator('tbody tr[data-testid^="row-"]')
  await expect(rows).toHaveCount(batch['明细总数'])

  // 列表入口展示的批次、实验编号与规则版本必须与后端一致。
  const firstRow = rows.first()
  const cells = await firstRow.locator('td').allInnerTexts()
  expect(cells[0]).toBe(batch['缓存批次'])
  expect(cells[1]).toBe('EXPE-0001')
  expect(cells[8]).toMatch(/^RV-/)

  // 详情面板读取同一份明细，字段与列表保持一致。
  await firstRow.getByTestId(/^detail-/).click()
  const detail = page.getByTestId('allocation-detail')
  await expect(detail).toBeVisible()
  await expect(detail.getByTestId('detail-分配批次')).toHaveText(cells[0])
  await expect(detail.getByTestId('detail-实验编号')).toHaveText(cells[1])
  await expect(detail.getByTestId('detail-缓存批次')).toHaveText(cells[7])
  await expect(detail.getByTestId('detail-规则版本')).toHaveText(cells[8])

  await page.getByTestId('close-detail').click()
  await expect(detail).toHaveCount(0)
  expect(browserErrors).toEqual([])
})

test('因子约束分配：状态筛选与导出', async ({ page }) => {
  await generateFull(page)
  await page.goto('/factor_allocation')
  await expect(page.locator('tbody tr[data-testid^="row-"]').first()).toBeVisible()

  await page.getByTestId('filter-state').selectOption('已回执')
  await page.getByTestId('query').click()
  await expect(page.locator('tbody tr[data-testid^="row-"]').first()).toHaveAttribute('data-status', '已回执')

  await page.getByTestId('reset').click()

  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('export').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('factor_allocation.csv')
})

test('因子约束分配：只改一个因子配置只重算受影响组且缓存批次可查', async ({ page }) => {
  await generateFull(page)
  const batchResponse = await page.request.get('/api/factor_allocation/status')
  const before = (await batchResponse.json())['批次'] as Record<string, any>
  const beforeRows = await listRows(page)
  const uiRowsBefore = beforeRows.filter((row) => row.因子编号 === 'EXPR-0003' || row.因子编号 === 'EXPR-0001')

  // 只改算法层的因子配置，UI 层应保持原样。
  const edit = await page.request.put('/api/expr_factor/2', { data: { 分配比例: '7:3' } })
  expect(edit.ok()).toBeTruthy()
  const payload = await edit.json()
  expect(payload.message).toContain('算法层')

  const afterRows = await listRows(page)
  const uiRowsAfter = afterRows.filter((row) => row.因子编号 === 'EXPR-0003' || row.因子编号 === 'EXPR-0001')
  expect(uiRowsAfter).toEqual(uiRowsBefore)

  const algoRows = afterRows.filter((row) => row.因子编号 === 'EXPR-0002')
  expect(algoRows.every((row) => row.缓存批次 === before['缓存批次'])).toBe(true)

  // 页面上的批次状态仍可查，且已完成组保持。
  await page.goto('/factor_allocation')
  await expect(page.getByTestId('batch-no')).toHaveText(before['缓存批次'])
  await expect(page.getByTestId('completed-groups')).toContainText('UI层')
})

test('因子约束分配：指标定义页读取分配表因子层', async ({ page }) => {
  const batch = await generateFull(page)
  await page.goto('/metric_def')
  const panel = page.getByTestId('factor-layers')
  await expect(panel).toBeVisible()
  await expect(panel).toContainText(batch['缓存批次'])
  await expect(page.getByTestId('layer-UI层')).toContainText('EXPE-0001')
  await expect(page.getByTestId('layer-算法层')).toContainText('EXPE-0002')
})
