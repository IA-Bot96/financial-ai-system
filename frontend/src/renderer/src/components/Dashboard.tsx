import { useEffect, useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { useApp } from '@/store'
import { api, type SeriesResponse } from '@/api'
import { fmt } from '@/lib/format'
import { Button } from './ui/Button'

const GRAPH_TYPES = ['Key ratios', 'Profitability', 'Balance sheet', 'Working capital'] as const
type GraphType = (typeof GRAPH_TYPES)[number]

const AXIS = '#9aa4b2'
const GRID = '#2a2f3a'
const PALETTE = ['#4f8cff', '#3fb950', '#d29922', '#a371f7', '#f85149']

export function Dashboard() {
  const session = useApp((s) => s.session)
  const toast = useApp((s) => s.toast)
  const [data, setData] = useState<SeriesResponse | null>(null)
  const [appliedTypes, setAppliedTypes] = useState<GraphType[]>([...GRAPH_TYPES])
  const [appliedYears, setAppliedYears] = useState<number[]>([])
  const [pendTypes, setPendTypes] = useState<GraphType[]>([...GRAPH_TYPES])
  const [pendYears, setPendYears] = useState<number[]>([])

  useEffect(() => {
    if (!session) return
    api.series(session.session_id).then((r) => {
      if (r.status === 200) {
        setData(r.body)
        setAppliedYears(r.body.years)
        setPendYears(r.body.years)
      } else {
        toast('error', 'Could not load dashboard data')
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.session_id])

  const years = useMemo(() => appliedYears.slice().sort((a, b) => a - b), [appliedYears])
  const val = (m: string, y: number): number | null => data?.series?.[m]?.[String(y)] ?? null
  const ratio = (a: string, b: string, y: number): number | null => {
    const x = val(a, y)
    const z = val(b, y)
    return x != null && z ? x / z : null
  }

  if (!data) {
    return <div className="h-full flex items-center justify-center text-sm text-muted">Loading dashboard…</div>
  }

  const latest = years[years.length - 1]
  const prev = years[years.length - 2]
  const rev = latest != null ? val('revenue', latest) : null
  const pat = latest != null ? val('pat', latest) : null
  const netMargin = latest != null ? ratio('pat', 'revenue', latest) : null
  const revPrev = prev != null ? val('revenue', prev) : null
  const revYoY = rev != null && revPrev ? (rev - revPrev) / revPrev : null

  return (
    <div className="h-full overflow-auto bg-bg p-4 space-y-4">
      {/* KPI row */}
      <div className="grid grid-cols-4 gap-3">
        <Kpi label={`Revenue (${latest ?? '—'})`} value={fmt(rev, 'currency')} />
        <Kpi label={`PAT (${latest ?? '—'})`} value={fmt(pat, 'currency')} />
        <Kpi label="Net margin" value={fmt(netMargin, 'percent')} />
        <Kpi label="Revenue YoY" value={fmt(revYoY, 'percent')} />
      </div>

      {/* filters */}
      <div className="flex flex-wrap items-end gap-4 rounded-lg border border-line bg-panel px-4 py-3">
        <Multi label="Graph type" options={[...GRAPH_TYPES]} selected={pendTypes} onToggle={(o) =>
          setPendTypes((s) => (s.includes(o as GraphType) ? s.filter((x) => x !== o) : [...s, o as GraphType]))
        } />
        <Multi label="Year" options={data.years.map(String)} selected={pendYears.map(String)} onToggle={(o) =>
          setPendYears((s) => (s.includes(Number(o)) ? s.filter((x) => x !== Number(o)) : [...s, Number(o)]))
        } />
        <div className="flex gap-2">
          <Button onClick={() => { setAppliedTypes(pendTypes); setAppliedYears(pendYears) }}>Apply</Button>
          <Button variant="subtle" onClick={() => {
            setPendTypes([...GRAPH_TYPES]); setPendYears(data.years)
            setAppliedTypes([...GRAPH_TYPES]); setAppliedYears(data.years)
          }}>Reset</Button>
        </div>
      </div>

      {/* charts */}
      <div className="grid grid-cols-2 gap-4">
        {appliedTypes.includes('Profitability') && (
          <Chart title="Profitability" option={barOption('Profitability', years, [
            { name: 'Revenue', data: years.map((y) => val('revenue', y)) },
            { name: 'PAT', data: years.map((y) => val('pat', y)) }
          ])} />
        )}
        {appliedTypes.includes('Key ratios') && (
          <Chart title="Key ratios (margins %)" option={lineOption('Key ratios', years, [
            { name: 'Gross margin', data: years.map((y) => pct(ratio('gross_profit', 'revenue', y))) },
            { name: 'Operating margin', data: years.map((y) => pct(ratio('operating_profit', 'revenue', y))) },
            { name: 'Net margin', data: years.map((y) => pct(ratio('pat', 'revenue', y))) }
          ], '%')} />
        )}
        {appliedTypes.includes('Balance sheet') && (
          <Chart title="Balance sheet" option={barOption('Balance sheet', years, [
            { name: 'Total assets', data: years.map((y) => val('total_assets', y)) },
            { name: 'Total equity', data: years.map((y) => val('total_equity', y)) }
          ])} />
        )}
        {appliedTypes.includes('Working capital') && (
          <Chart title="Working capital" option={barOption('Working capital', years, [
            { name: 'Current assets', data: years.map((y) => val('current_assets', y)) },
            { name: 'Current liabilities', data: years.map((y) => val('current_liabilities', y)) },
            {
              name: 'Working capital',
              data: years.map((y) => {
                const ca = val('current_assets', y)
                const cl = val('current_liabilities', y)
                return ca != null && cl != null ? ca - cl : null
              })
            }
          ])} />
        )}
      </div>
    </div>
  )
}

const pct = (r: number | null) => (r == null ? null : +(r * 100).toFixed(2))

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="text-xl font-semibold mt-1">{value}</div>
    </div>
  )
}

function Multi({
  label,
  options,
  selected,
  onToggle
}: {
  label: string
  options: string[]
  selected: string[]
  onToggle: (o: string) => void
}) {
  return (
    <div>
      <div className="text-xs text-muted mb-1">{label}</div>
      <div className="flex flex-wrap gap-1.5 max-w-[420px]">
        {options.map((o) => {
          const on = selected.includes(o)
          return (
            <button
              key={o}
              onClick={() => onToggle(o)}
              className={
                'text-xs rounded-full px-2.5 py-1 border ' +
                (on ? 'bg-accent/20 text-accent border-accent/50' : 'bg-panel2 text-muted border-line')
              }
            >
              {o}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function Chart({ title, option }: { title: string; option: object }) {
  return (
    <div className="rounded-lg border border-line bg-panel p-3">
      <div className="text-sm font-medium mb-2">{title}</div>
      <ReactECharts option={option} style={{ height: 280 }} notMerge />
    </div>
  )
}

type Series = { name: string; data: (number | null)[] }

function baseOption(years: number[], series: Series[], unit = '') {
  return {
    backgroundColor: 'transparent',
    color: PALETTE,
    textStyle: { color: AXIS },
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: AXIS }, top: 0 },
    grid: { left: 56, right: 16, top: 32, bottom: 28 },
    toolbox: { feature: { saveAsImage: { backgroundColor: '#0f1115', title: 'PNG' } }, right: 8 },
    xAxis: {
      type: 'category',
      data: years.map(String),
      axisLine: { lineStyle: { color: GRID } },
      axisLabel: { color: AXIS }
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: AXIS, formatter: unit === '%' ? `{value}%` : '{value}' },
      splitLine: { lineStyle: { color: GRID } }
    },
    series
  }
}

function barOption(_t: string, years: number[], s: Series[]) {
  return baseOption(years, s.map((x) => ({ ...x, type: 'bar' })) as never)
}
function lineOption(_t: string, years: number[], s: Series[], unit = '') {
  return baseOption(years, s.map((x) => ({ ...x, type: 'line', smooth: true })) as never, unit)
}
