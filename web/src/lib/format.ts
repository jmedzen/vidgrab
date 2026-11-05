/** 1:23 / 0:01:23 */
export function fmtSec(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}

/** 「0:15」→ 15（給片段節點用） */
export function tsToSec(value: string): number {
  const parts = value.split(':').map((x) => Number(x))
  if (parts.some(Number.isNaN)) return 0
  return parts.reduce((acc, n) => acc * 60 + n, 0)
}

export const LIC_CLASS: Record<string, string> = {
  free: 'lic-free',
  'free-attribution': 'lic-free',
  check: 'lic-check',
  unknown: 'lic-unknown',
  none: 'lic-unknown',
}

export const LIC_LABEL: Record<string, string> = {
  free: '可自由使用',
  'free-attribution': '可用需標註',
  check: '需確認授權',
  unknown: '未知',
  none: '無資訊',
}
