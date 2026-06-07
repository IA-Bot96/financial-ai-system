/**
 * Shared number formatter (frontend-spec §10): thousands separators, negatives in
 * parentheses, ratios as `x`, percentages 1dp. Used by Sheet/Dashboard/Ask AI so figures
 * read identically everywhere. Base scale is "Rupees in thousand".
 */
export type FmtKind = 'currency' | 'x' | 'percent' | 'number'

export function fmt(value: number | null | undefined, kind: FmtKind = 'number'): string {
  if (value == null || Number.isNaN(value)) return 'n/a'
  if (kind === 'x') return `${value.toFixed(2)}x`
  if (kind === 'percent') return `${(value * 100).toFixed(1)}%`
  const neg = value < 0
  const s = Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 0 })
  return neg ? `(${s})` : s
}
