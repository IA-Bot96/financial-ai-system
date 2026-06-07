import { useEffect, useMemo, useRef, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { useApp } from '@/store'
import { api, type SeriesResponse } from '@/api'
import { fmt } from '@/lib/format'

// ---- constants -----------------------------------------------------------------------
const SECTIONS = [
  'Activity turnover ratios',
  'Liquidity ratios',
  'Key ratios',
  'Balance sheet',
  'Working capital',
  'Cash flow',
  'Revenue & profit',
  'Investment ratios',
  'Profitability ratios'
] as const
type Section = (typeof SECTIONS)[number]

const AXIS = '#9aa4b2'
const GRID = '#2a2f3a'
const YELLOW = '#e3b341'
const BLUE = '#4f8cff'
const GREEN = '#3fb950'
const GRAY = '#8b949e'
const PURPLE = '#a371f7'
const PALETTE = [YELLOW, BLUE, GREEN, PURPLE, '#f85149', GRAY]

// ---- Dashboard component -------------------------------------------------------------
export function Dashboard() {
  const session = useApp((s) => s.session)
  const toast = useApp((s) => s.toast)
  const [data, setData] = useState<SeriesResponse | null>(null)
  // empty = no filter active → show all
  const [filterSections, setFilterSections] = useState<string[]>([])
  const [filterYears, setFilterYears] = useState<string[]>([])

  useEffect(() => {
    if (!session) return
    api.series(session.session_id).then((r) => {
      if (r.status !== 200) {
        toast('error', 'Could not load dashboard data')
        return
      }
      setData(r.body)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.session_id])

  // years that actually carry data (forecast cells are blank → skip them)
  const allDataYears = useMemo(() => {
    if (!data) return []
    return data.years
      .filter((y) => Object.values(data.series).some((row) => row[String(y)] != null))
      .sort((a, b) => a - b)
  }, [data])

  // apply year filter (empty filterYears = show all data years)
  const years = useMemo(() => {
    if (filterYears.length === 0) return allDataYears
    const set = new Set(filterYears)
    return allDataYears.filter((y) => set.has(String(y)))
  }, [allDataYears, filterYears])

  // ---- data accessors ---------------------------------------------------------------
  const val = (m: string, y: number): number | null => data?.series?.[m]?.[String(y)] ?? null
  const ratio = (a: string, b: string, y: number): number | null => {
    const x = val(a, y)
    const z = val(b, y)
    return x != null && z ? x / z : null
  }
  const r2 = (v: number | null): number | null => (v == null ? null : +v.toFixed(2))
  const pct = (r: number | null): number | null => (r == null ? null : +(r * 100).toFixed(2))

  // ---- derived series ---------------------------------------------------------------
  const tl = (y: number): number | null => {
    const cl = val('current_liabilities', y)
    const ncl = val('non_current_liabilities', y)
    if (cl != null && ncl != null) return cl + ncl
    const tel = val('total_equity_and_liabilities', y)
    const te = val('total_equity', y)
    return tel != null && te != null ? tel - te : null
  }
  const ebitda = (y: number): number | null => {
    const op = val('operating_profit', y)
    if (op == null) return null
    return op + (val('depreciation_expense', y) ?? 0)
  }
  const wc = (y: number): number | null => {
    const ca = val('current_assets', y)
    const cl = val('current_liabilities', y)
    return ca != null && cl != null ? ca - cl : null
  }

  // activity turnover
  const inventoryTurnover = (y: number): number | null => {
    const cogs = val('cost_of_sales', y)
    const inv = val('stock_in_trade', y)
    return cogs != null && inv ? cogs / inv : null
  }
  const assetTurnover = (y: number): number | null => ratio('revenue', 'total_assets', y)
  const fixedAssetTurnover = (y: number): number | null => ratio('revenue', 'non_current_assets', y)
  const debtorTurnover = (y: number): number | null => ratio('revenue', 'trade_debts', y)
  const creditorTurnover = (y: number): number | null => {
    const cogs = val('cost_of_sales', y)
    const pay = val('creditors_accrued_other_liabilities', y)
    return cogs != null && pay ? cogs / pay : null
  }
  const daysInventory = (y: number): number | null => {
    const t = inventoryTurnover(y)
    return t ? +(365 / t).toFixed(2) : null
  }
  const daysReceivables = (y: number): number | null => {
    const t = debtorTurnover(y)
    return t ? +(365 / t).toFixed(2) : null
  }
  const daysPayables = (y: number): number | null => {
    const t = creditorTurnover(y)
    return t ? +(365 / t).toFixed(2) : null
  }

  // liquidity
  const quickRatio = (y: number): number | null => {
    const ca = val('current_assets', y)
    const inv = val('stock_in_trade', y)
    const cl = val('current_liabilities', y)
    return ca != null && inv != null && cl ? (ca - inv) / cl : null
  }

  // year-over-year % change of any derived series
  const yoySeries = (get: (y: number) => number | null): (number | null)[] =>
    years.map((y, i) => {
      if (i === 0) return null
      const cur = get(y)
      const prev = get(years[i - 1])
      return cur != null && prev ? +(((cur - prev) / Math.abs(prev)) * 100).toFixed(2) : null
    })

  // cumulative CAGR % from the first year
  const cagrSeries = (m: string): (number | null)[] => {
    const base = val(m, years[0])
    return years.map((y, i) => {
      if (i === 0 || base == null || base <= 0) return null
      const cur = val(m, y)
      return cur != null && cur > 0 ? +((Math.pow(cur / base, 1 / i) - 1) * 100).toFixed(2) : null
    })
  }

  if (!data) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-muted">
        Loading dashboard…
      </div>
    )
  }

  const latest = years[years.length - 1]
  const prev = years[years.length - 2]
  const rev = latest != null ? val('revenue', latest) : null
  const pat = latest != null ? val('pat', latest) : null
  const netMargin = latest != null ? ratio('pat', 'revenue', latest) : null
  const revPrev = prev != null ? val('revenue', prev) : null
  const revYoY = rev != null && revPrev ? (rev - revPrev) / revPrev : null

  const show = (s: Section): boolean =>
    filterSections.length === 0 || filterSections.includes(s)

  return (
    <div className="h-full overflow-auto bg-bg p-4 space-y-4">
      {/* KPI row — uses the latest year that actually has data */}
      <div className="grid grid-cols-4 gap-3">
        <Kpi label={`Revenue (${latest ?? '—'})`} value={fmt(rev, 'currency')} />
        <Kpi label={`PAT (${latest ?? '—'})`} value={fmt(pat, 'currency')} />
        <Kpi label="Net margin" value={fmt(netMargin, 'percent')} />
        <Kpi label="Revenue YoY" value={fmt(revYoY, 'percent')} />
      </div>

      {/* filter bar — dropdown style with search + multi-select */}
      <div className="flex items-center gap-3 rounded-lg border border-line bg-panel px-4 py-2.5">
        <span className="text-xs font-medium text-muted shrink-0">Filter</span>
        <FilterDropdown
          label="Section"
          options={[...SECTIONS]}
          selected={filterSections}
          onChange={setFilterSections}
          toast={toast}
        />
        <FilterDropdown
          label="Year"
          options={allDataYears.map(String)}
          selected={filterYears}
          onChange={setFilterYears}
          toast={toast}
        />
        {(filterSections.length > 0 || filterYears.length > 0) && (
          <button
            onClick={() => {
              setFilterSections([])
              setFilterYears([])
            }}
            className="ml-1 text-xs text-muted hover:text-ink underline underline-offset-2 transition-colors"
          >
            Reset
          </button>
        )}
      </div>

      {/* ---- ACTIVITY TURNOVER RATIOS ---- */}
      {show('Activity turnover ratios') && (
        <Group title="Activity turnover ratios" className="space-y-4">
          {/* top row: two full-width charts */}
          <div className="grid grid-cols-2 gap-4">
            <Chart
              title="Total Assets, Fixed Asset & Inventory Turnover"
              option={barOpt(years, [
                { name: 'Assets', data: years.map((y) => r2(assetTurnover(y))) },
                { name: 'Inventory', data: years.map((y) => r2(inventoryTurnover(y))) },
                { name: 'Fixed Assets', data: years.map((y) => r2(fixedAssetTurnover(y))) }
              ])}
            />
            <Chart
              title="Debtor vs Creditor Turnover"
              option={mixedOpt(years,
                [{ name: 'Creditor turnover', data: years.map((y) => r2(creditorTurnover(y))) }],
                [{ name: 'Debtor turnover', data: years.map((y) => r2(debtorTurnover(y))) }]
              )}
            />
          </div>
          {/* bottom row: 3 KPI sparkline cards in equal columns */}
          <div className="grid grid-cols-3 gap-4">
            <SparklineKpi
              label="No. of Days in Inventory"
              years={years}
              data={years.map((y) => daysInventory(y))}
              latestYear={latest}
              latestValue={latest != null ? daysInventory(latest) : null}
            />
            <SparklineKpi
              label="No. of Days in Receivables"
              years={years}
              data={years.map((y) => daysReceivables(y))}
              latestYear={latest}
              latestValue={latest != null ? daysReceivables(latest) : null}
            />
            <SparklineKpi
              label="No. of Days in Creditors"
              years={years}
              data={years.map((y) => daysPayables(y))}
              latestYear={latest}
              latestValue={latest != null ? daysPayables(latest) : null}
            />
          </div>
        </Group>
      )}

      {/* ---- LIQUIDITY RATIOS ---- */}
      {show('Liquidity ratios') && (
        <Group title="Liquidity ratios">
          <Chart
            title="Current Ratio"
            option={lineOptRef(years, [
              {
                name: 'Current ratio',
                data: years.map((y) => r2(ratio('current_assets', 'current_liabilities', y))),
                area: true
              }
            ], '', 1)}
          />
          <Chart
            title="Quick Ratio"
            option={lineOptRef(years, [
              { name: 'Quick ratio', data: years.map((y) => r2(quickRatio(y))), area: true }
            ], '', 1)}
          />
          <Chart
            title="Cash to Current Liability"
            option={lineOpt(years, [
              {
                name: 'Cash / CL',
                data: years.map((y) => r2(ratio('cash_and_bank', 'current_liabilities', y))),
                area: true
              }
            ])}
          />
          <Chart
            title="Operating Profit to Revenue by Year"
            option={donutByYearOpt(
              years,
              years.map((y) => pct(ratio('operating_profit', 'revenue', y)))
            )}
          />
        </Group>
      )}

      {/* ---- KEY RATIOS ---- */}
      {show('Key ratios') && (
        <Group title="Key ratios">
          <Chart
            title="Current Ratio"
            option={lineOpt(years, [
              {
                name: 'Current ratio',
                data: years.map((y) => r2(ratio('current_assets', 'current_liabilities', y))),
                area: true
              }
            ])}
          />
          <Chart
            title="Debtor vs Creditor Turnover"
            option={lineOpt(years, [
              { name: 'Debtor turnover', data: years.map((y) => r2(ratio('revenue', 'trade_debts', y))) },
              {
                name: 'Creditor turnover',
                data: years.map((y) =>
                  r2(ratio('cost_of_sales', 'creditors_accrued_other_liabilities', y))
                )
              }
            ])}
          />
          <Chart
            title="Debt vs Asset"
            option={lineOpt(years, [
              {
                name: 'Debt / Assets',
                data: years.map((y) => r2(safeDiv(tl(y), val('total_assets', y)))),
                area: true
              }
            ])}
          />
          <Chart
            title="Dividend Coverage Ratio"
            option={barOpt(years, [
              {
                name: 'Coverage (PAT / Dividends)',
                data: years.map((y) => r2(ratio('pat', 'dividends_paid', y)))
              }
            ])}
          />
          <Chart
            title="Operating Income (%)"
            option={lineOpt(years, [
              {
                name: 'Operating margin',
                data: years.map((y) => pct(ratio('operating_profit', 'revenue', y))),
                area: true
              }
            ], '%')}
          />
          <Chart
            title="EBITDA to Sales (%)"
            option={lineOpt(years, [
              {
                name: 'EBITDA margin',
                data: years.map((y) => pct(safeDiv(ebitda(y), val('revenue', y)))),
                area: true
              }
            ], '%')}
          />
        </Group>
      )}

      {/* ---- BALANCE SHEET ---- */}
      {show('Balance sheet') && (
        <Group title="Balance sheet">
          <Chart
            title="Net Assets"
            option={comboOpt(
              years,
              [{ name: 'Net assets', data: years.map((y) => val('total_equity', y)) }],
              [{ name: 'Change %', data: yoySeries((y) => val('total_equity', y)) }],
              'M', '%'
            )}
          />
          <Chart
            title="Total Liabilities vs Total Assets"
            option={barOpt(years, [
              { name: 'Total liabilities', data: years.map((y) => tl(y)) },
              { name: 'Total assets', data: years.map((y) => val('total_assets', y)) }
            ], 'M')}
          />
          <Chart
            title="Debt vs Equity (%)"
            option={lineOpt(years, [
              {
                name: 'Debt %',
                data: years.map((y) =>
                  pct(safeDiv(tl(y), (val('total_equity', y) ?? 0) + (tl(y) ?? 0)))
                ),
                area: true
              },
              {
                name: 'Equity %',
                data: years.map((y) =>
                  pct(safeDiv(val('total_equity', y), (val('total_equity', y) ?? 0) + (tl(y) ?? 0)))
                )
              }
            ], '%')}
          />
          <Chart
            title={`Current vs Non-Current Assets (${latest ?? '—'})`}
            option={donutOpt([
              { name: 'Current assets', value: (latest != null && val('current_assets', latest)) || 0 },
              { name: 'Non-current assets', value: (latest != null && val('non_current_assets', latest)) || 0 }
            ])}
          />
          <Chart
            title={`Current vs Non-Current Liabilities (${latest ?? '—'})`}
            option={donutOpt([
              { name: 'Current liabilities', value: (latest != null && val('current_liabilities', latest)) || 0 },
              {
                name: 'Non-current liabilities',
                value: (latest != null && val('non_current_liabilities', latest)) || 0
              }
            ])}
          />
        </Group>
      )}

      {/* ---- WORKING CAPITAL ---- */}
      {show('Working capital') && (
        <Group title="Working capital">
          <Chart
            title="Working Capital"
            option={comboOpt(
              years,
              [{ name: 'Working capital', data: years.map((y) => wc(y)) }],
              [{ name: 'Change %', data: yoySeries((y) => wc(y)) }],
              'M', '%'
            )}
          />
          <Chart
            title="Assets vs Liabilities"
            option={barOpt(years, [
              { name: 'Total assets', data: years.map((y) => val('total_assets', y)) },
              { name: 'Total liabilities', data: years.map((y) => tl(y)) }
            ], 'M')}
          />
          <Chart
            title="Receivable vs Payable vs Inventory"
            option={barOpt(years, [
              { name: 'Receivables', data: years.map((y) => val('trade_debts', y)) },
              {
                name: 'Payables',
                data: years.map((y) => val('creditors_accrued_other_liabilities', y))
              },
              { name: 'Inventory', data: years.map((y) => val('stock_in_trade', y)) }
            ], 'M')}
          />
        </Group>
      )}

      {/* ---- CASH FLOW ---- */}
      {show('Cash flow') && (
        <Group title="Cash flow">
          <Chart
            title="Cash Balances"
            option={barOpt(years, [
              { name: 'Cash & bank', data: years.map((y) => val('cash_and_bank', y)) }
            ], 'M')}
          />
          <Chart
            title="Cash Ratio"
            option={lineOpt(years, [
              {
                name: 'Cash ratio',
                data: years.map((y) => pct(ratio('cash_and_bank', 'current_liabilities', y))),
                area: true
              }
            ], '%')}
          />
        </Group>
      )}

      {/* ---- REVENUE & PROFIT ---- */}
      {show('Revenue & profit') && (
        <Group title="Revenue & profit">
          <Chart
            title="COS, Admin & Distribution as % of Net Revenue"
            option={comboOpt(
              years,
              [{ name: 'Net revenue', data: years.map((y) => val('revenue', y)) }],
              [
                { name: 'Cost of sales %', data: years.map((y) => pct(ratio('cost_of_sales', 'revenue', y))) },
                {
                  name: 'Admin %',
                  data: years.map((y) => pct(ratio('administrative_expenses', 'revenue', y)))
                },
                {
                  name: 'Distribution %',
                  data: years.map((y) =>
                    pct(ratio('distribution_marketing_expenses', 'revenue', y))
                  )
                }
              ],
              'M', '%'
            )}
          />
          <Chart
            title="Debt & Equity"
            option={comboOpt(
              years,
              [
                { name: 'Total equity', data: years.map((y) => val('total_equity', y)) },
                { name: 'Total debt', data: years.map((y) => tl(y)) }
              ],
              [
                {
                  name: 'Debt %',
                  data: years.map((y) =>
                    pct(safeDiv(tl(y), (val('total_equity', y) ?? 0) + (tl(y) ?? 0)))
                  )
                },
                {
                  name: 'Equity %',
                  data: years.map((y) =>
                    pct(safeDiv(val('total_equity', y), (val('total_equity', y) ?? 0) + (tl(y) ?? 0)))
                  )
                }
              ],
              'M', '%'
            )}
          />
          <Chart
            title="Net Revenue, YoY Growth & CAGR"
            option={comboOpt(
              years,
              [{ name: 'Net revenue', data: years.map((y) => val('revenue', y)) }],
              [
                { name: 'YoY %', data: yoySeries((y) => val('revenue', y)) },
                { name: 'CAGR %', data: cagrSeries('revenue') }
              ],
              'M', '%'
            )}
          />
          <Chart
            title="Net Revenue, Gross Profit, EBITDA, Net Profit"
            option={comboOpt(
              years,
              [
                { name: 'Net revenue', data: years.map((y) => val('revenue', y)) },
                { name: 'Gross profit', data: years.map((y) => val('gross_profit', y)) },
                { name: 'EBITDA', data: years.map((y) => ebitda(y)) },
                { name: 'Net profit', data: years.map((y) => val('pat', y)) }
              ],
              [
                { name: 'GP margin %', data: years.map((y) => pct(ratio('gross_profit', 'revenue', y))) },
                { name: 'NP margin %', data: years.map((y) => pct(ratio('pat', 'revenue', y))) }
              ],
              'M', '%'
            )}
          />
        </Group>
      )}

      {/* ---- INVESTMENT RATIOS ---- */}
      {show('Investment ratios') && (
        <Group title="Investment ratios">
          <Chart
            title="Debt to Equity (% of capital)"
            option={stack100Opt(years, [
              {
                name: 'Equity %',
                data: years.map((y) =>
                  pct(safeDiv(val('total_equity', y), (val('total_equity', y) ?? 0) + (tl(y) ?? 0)))
                )
              },
              {
                name: 'Debt %',
                data: years.map((y) =>
                  pct(safeDiv(tl(y), (val('total_equity', y) ?? 0) + (tl(y) ?? 0)))
                )
              }
            ])}
          />
          <Chart
            title="Dividend Coverage"
            option={barOpt(years, [
              {
                name: 'Coverage (PAT / Dividends)',
                data: years.map((y) => r2(ratio('pat', 'dividends_paid', y)))
              }
            ])}
          />
          <Chart
            title="Dividend Payout (%)"
            option={lineOpt(years, [
              {
                name: 'Payout %',
                data: years.map((y) => pct(ratio('dividends_paid', 'pat', y))),
                area: true
              }
            ], '%')}
          />
        </Group>
      )}

      {/* ---- PROFITABILITY RATIOS ---- */}
      {show('Profitability ratios') && (
        <Group title="Profitability ratios">
          <Chart
            title="Gross Profit Margin (%)"
            option={lineOpt(years, [
              {
                name: 'Gross margin',
                data: years.map((y) => pct(ratio('gross_profit', 'revenue', y))),
                area: true
              }
            ], '%')}
          />
          <Chart
            title="EBITDA Margin (%)"
            option={lineOpt(years, [
              {
                name: 'EBITDA margin',
                data: years.map((y) => pct(safeDiv(ebitda(y), val('revenue', y)))),
                area: true
              }
            ], '%')}
          />
          <Chart
            title="Net Profit Margin (%)"
            option={lineOpt(years, [
              {
                name: 'Net margin',
                data: years.map((y) => pct(ratio('pat', 'revenue', y))),
                area: true
              }
            ], '%')}
          />
          <Chart
            title="Return on Capital Employed (%)"
            option={barOpt(years, [
              {
                name: 'RoCE',
                data: years.map((y) =>
                  pct(
                    safeDiv(
                      val('operating_profit', y),
                      capEmployed(val('total_assets', y), val('current_liabilities', y))
                    )
                  )
                )
              }
            ], '%')}
          />
          <Chart
            title="Return on Assets (%)"
            option={lineOpt(years, [
              {
                name: 'RoA',
                data: years.map((y) => pct(ratio('pat', 'total_assets', y))),
                area: true
              }
            ], '%')}
          />
        </Group>
      )}

      <p className="text-xs text-muted pt-1">
        Forecast years (no values in this workbook) are hidden. Dividend Yield, P/E and EPS need
        a share-price source not present in the financials — pending.
      </p>
    </div>
  )
}

// ---- small math helpers --------------------------------------------------------------
const safeDiv = (a: number | null, b: number | null): number | null =>
  a != null && b ? a / b : null
const capEmployed = (assets: number | null, cl: number | null): number | null =>
  assets != null && cl != null ? assets - cl : null

// ---- presentational components -------------------------------------------------------
function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="text-xl font-semibold mt-1">{value}</div>
    </div>
  )
}

/**
 * KPI card with a small sparkline area chart underneath the big number.
 * Used for "No. of Days in …" cards in the Activity Turnover section.
 */
function SparklineKpi({
  label,
  years,
  data,
  latestYear,
  latestValue
}: {
  label: string
  years: number[]
  data: (number | null)[]
  latestYear: number | undefined
  latestValue: number | null
}) {
  const opt = {
    backgroundColor: 'transparent',
    grid: { left: 0, right: 0, top: 2, bottom: 0 },
    xAxis: { type: 'category' as const, data: years.map(String), show: false },
    yAxis: { type: 'value' as const, show: false },
    series: [
      {
        type: 'line',
        data,
        smooth: true,
        symbol: 'none',
        lineStyle: { color: GRAY, width: 1.5 },
        areaStyle: { color: GRAY, opacity: 0.13 }
      }
    ]
  }
  return (
    <div className="rounded-lg border border-line bg-panel px-3 pt-3 pb-0 flex flex-col">
      <div className="text-[10px] text-muted leading-tight mb-1">{label}</div>
      <div className="text-2xl font-semibold tabular-nums">
        {latestValue != null ? latestValue.toFixed(2) : '—'}
      </div>
      <div className="text-[10px] text-muted mt-0.5 mb-1">{latestYear ?? '—'}</div>
      <ReactECharts option={opt} style={{ height: 56 }} notMerge />
    </div>
  )
}

function Group({
  title,
  children,
  className
}: {
  title: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase tracking-wide text-muted mb-2">{title}</div>
      <div className={className ?? 'grid grid-cols-2 gap-4'}>{children}</div>
    </div>
  )
}

function Chart({ title, option }: { title: string; option: object }) {
  return (
    <div className="rounded-lg border border-line bg-panel p-3">
      <div className="text-sm font-medium mb-2">{title}</div>
      <ReactECharts option={option} style={{ height: 260 }} notMerge />
    </div>
  )
}

/**
 * Searchable multi-select dropdown.
 * `selected = []` means "no filter active → show all".
 */
function FilterDropdown({
  label,
  options,
  selected,
  onChange,
  toast
}: {
  label: string
  options: string[]
  selected: string[]
  onChange: (v: string[]) => void
  toast: (kind: 'info' | 'warning' | 'error' | 'success', text: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const ref = useRef<HTMLDivElement>(null)

  // click-outside closes the dropdown; clear search on close
  useEffect(() => {
    if (!open) {
      setSearch('')
      return
    }
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const filtered = options.filter((o) => o.toLowerCase().includes(search.toLowerCase()))

  // empty selected = no filter active = "all selected" visually
  const allShown = selected.length === 0
  const isChecked = (o: string) => selected.length === 0 || selected.includes(o)

  const toggle = (o: string) => {
    if (selected.length === 0) {
      // all are currently shown → unchecking one means "show all except this".
      // (Only blocked if there's a single option total, which can't go to zero.)
      if (options.length <= 1) {
        toast('error', `At least one ${label.toLowerCase()} must stay selected.`)
        return
      }
      onChange(options.filter((x) => x !== o))
    } else if (selected.includes(o)) {
      const next = selected.filter((x) => x !== o)
      if (next.length === 0) {
        // deselecting the last remaining one — not allowed; keep it selected.
        toast('error', `At least one ${label.toLowerCase()} must stay selected.`)
        return
      }
      onChange(next)
    } else {
      const next = [...selected, o]
      // if every option is now checked → clear filter (= show all)
      onChange(next.length === options.length ? [] : next)
    }
  }

  // "Select All" always resets to no-filter state
  const selectAll = () => onChange([])

  const displayLabel =
    selected.length === 0
      ? `All ${label}s`
      : selected.length === 1
        ? selected[0]
        : `${selected.length} ${label}s`

  return (
    <div className="relative" ref={ref}>
      {/* trigger button */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-line bg-panel2 text-sm text-ink hover:border-accent/40 transition-colors min-w-[148px] justify-between"
      >
        <span className="truncate max-w-[130px]">{displayLabel}</span>
        {/* chevron */}
        <svg
          viewBox="0 0 20 20"
          fill="currentColor"
          className="h-3.5 w-3.5 text-muted shrink-0 transition-transform"
          style={{ transform: open ? 'rotate(180deg)' : 'none' }}
        >
          <path
            fillRule="evenodd"
            d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
            clipRule="evenodd"
          />
        </svg>
      </button>

      {/* dropdown panel */}
      {open && (
        <div className="absolute top-full left-0 mt-1.5 z-50 w-[248px] rounded-lg border border-line bg-panel shadow-xl shadow-black/40">
          {/* search input */}
          <div className="p-2 border-b border-line">
            <input
              autoFocus
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={`Search ${label.toLowerCase()}…`}
              className="w-full rounded-md bg-panel2 border border-line px-2.5 py-1.5 text-sm text-ink placeholder:text-muted outline-none focus:border-accent/50 transition-colors"
            />
          </div>

          {/* item list */}
          <div className="max-h-[264px] overflow-y-auto p-1.5 pr-2 space-y-0.5">
            {/* Select All */}
            <label className="flex items-center gap-2.5 px-2 py-1.5 rounded-md hover:bg-line cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allShown}
                onChange={selectAll}
                className="h-3.5 w-3.5 accent-blue-400 rounded"
              />
              <span className="text-sm text-ink font-medium">Select All</span>
            </label>
            <div className="border-t border-line my-1" />

            {filtered.length === 0 && (
              <p className="text-xs text-muted px-2 py-2">No results for "{search}"</p>
            )}
            {filtered.map((o) => (
              <label
                key={o}
                className="flex items-center gap-2.5 px-2 py-1.5 rounded-md hover:bg-line cursor-pointer select-none"
              >
                <input
                  type="checkbox"
                  checked={isChecked(o)}
                  onChange={() => toggle(o)}
                  className="h-3.5 w-3.5 accent-blue-400 rounded"
                />
                <span className="text-sm text-muted">{o}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ---- ECharts option builders ---------------------------------------------------------
type S = { name: string; data: (number | null)[]; area?: boolean }

const yFormatter = (unit: string) =>
  unit === '%'
    ? '{value}%'
    : unit === 'M'
      ? (v: number) => `${(v / 1e6).toFixed(0)}M`
      : '{value}'

function frame(years: number[], yAxis: object | object[]) {
  return {
    backgroundColor: 'transparent',
    color: PALETTE,
    textStyle: { color: AXIS },
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: AXIS }, top: 0, type: 'scroll' },
    grid: { left: 56, right: 56, top: 34, bottom: 28 },
    toolbox: { feature: { saveAsImage: { backgroundColor: '#0f1115', title: 'PNG' } }, right: 8 },
    xAxis: {
      type: 'category',
      data: years.map(String),
      axisLine: { lineStyle: { color: GRID } },
      axisLabel: { color: AXIS }
    },
    yAxis
  }
}

function axisFor(unit: string, name = '') {
  return {
    type: 'value',
    name,
    axisLabel: { color: AXIS, formatter: yFormatter(unit) },
    splitLine: { lineStyle: { color: GRID } }
  }
}

function barOpt(years: number[], s: S[], unit = '') {
  return { ...frame(years, axisFor(unit)), series: s.map((x) => ({ ...x, type: 'bar' })) }
}

function lineOpt(years: number[], s: S[], unit = '') {
  return {
    ...frame(years, axisFor(unit)),
    series: s.map((x) => ({
      name: x.name,
      data: x.data,
      type: 'line',
      smooth: true,
      areaStyle: x.area ? { opacity: 0.18 } : undefined
    }))
  }
}

/** lineOpt + a horizontal dotted reference line at refVal (e.g. the 1.0 benchmark). */
function lineOptRef(years: number[], s: S[], unit = '', refVal?: number) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const opt = lineOpt(years, s, unit) as any
  if (refVal != null && Array.isArray(opt.series) && opt.series.length > 0) {
    opt.series[0] = {
      ...opt.series[0],
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { type: 'dotted', color: GRAY, width: 1.5 },
        label: { position: 'end', color: GRAY, formatter: String(refVal) },
        data: [{ yAxis: refVal }]
      }
    }
  }
  return opt
}

/** Single-axis bar + line mix — used for Debtor vs Creditor Turnover. */
function mixedOpt(years: number[], bars: S[], lines: S[], unit = '') {
  return {
    ...frame(years, axisFor(unit)),
    series: [
      ...bars.map((x) => ({ name: x.name, data: x.data, type: 'bar' })),
      ...lines.map((x) => ({ name: x.name, data: x.data, type: 'line', smooth: true }))
    ]
  }
}

/** Dual-axis: bars on left value axis, lines on right (% overlay). */
function comboOpt(years: number[], bars: S[], lines: S[], leftUnit = '', rightUnit = '%') {
  return {
    ...frame(years, [axisFor(leftUnit), axisFor(rightUnit)]),
    series: [
      ...bars.map((x) => ({ name: x.name, data: x.data, type: 'bar', yAxisIndex: 0 })),
      ...lines.map((x) => ({ name: x.name, data: x.data, type: 'line', smooth: true, yAxisIndex: 1 }))
    ]
  }
}

/** 100% horizontal stacked bars (one row per year). */
function stack100Opt(years: number[], s: S[]) {
  return {
    backgroundColor: 'transparent',
    color: [YELLOW, '#3a4150'],
    textStyle: { color: AXIS },
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: AXIS }, top: 0 },
    grid: { left: 48, right: 16, top: 34, bottom: 24 },
    xAxis: {
      type: 'value',
      max: 100,
      axisLabel: { color: AXIS, formatter: '{value}%' },
      splitLine: { lineStyle: { color: GRID } }
    },
    yAxis: {
      type: 'category',
      data: years.map(String),
      axisLine: { lineStyle: { color: GRID } },
      axisLabel: { color: AXIS }
    },
    series: s.map((x) => ({
      name: x.name,
      data: x.data,
      type: 'bar',
      stack: 'total',
      label: {
        show: true,
        color: '#0f1115',
        formatter: (p: { value: number }) => (p.value != null ? `${Math.round(p.value)}%` : '')
      }
    }))
  }
}

/** Classic donut — two slices (current vs non-current, debt vs equity etc.). */
function donutOpt(parts: { name: string; value: number }[]) {
  return {
    backgroundColor: 'transparent',
    color: [YELLOW, '#3a4150'],
    textStyle: { color: AXIS },
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { textStyle: { color: AXIS }, bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['45%', '70%'],
        center: ['50%', '46%'],
        label: { color: AXIS, formatter: '{d}%' },
        data: parts
      }
    ]
  }
}

/**
 * Donut that slices by year — each year's value becomes one slice.
 * Used for "Operating Profit to Revenue by Year" (proxy for CF/Sales pie in PBI).
 */
function donutByYearOpt(years: number[], data: (number | null)[]) {
  const parts = years
    .map((y, i) => ({ name: String(y), value: data[i] ?? 0 }))
    .filter((p) => p.value > 0)
  return {
    backgroundColor: 'transparent',
    color: PALETTE,
    textStyle: { color: AXIS },
    tooltip: { trigger: 'item', formatter: '{b}: {c}% ({d}%)' },
    legend: { textStyle: { color: AXIS }, bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['40%', '65%'],
        center: ['50%', '44%'],
        label: {
          color: AXIS,
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          formatter: (p: any) =>
            p.value != null ? `${Number(p.value).toFixed(1)}%\n(${p.percent?.toFixed(1)}%)` : ''
        },
        data: parts
      }
    ]
  }
}
