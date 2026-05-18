import React, { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, Legend,
} from 'recharts'
import {
  Users, TrendingUp, AlertTriangle, CheckCircle2,
  Search, ChevronLeft, ChevronRight, ArrowUpDown,
  Phone, MessageSquare, Mail,
} from 'lucide-react'
import Header from '../components/Header'
import { useData } from '../App'
import {
  fmtCur, tierBg, tierDot, sentimentColor, dpdShort, waiverInfo, scoreColor
} from '../lib/utils'

/* ── Constants ──────────────────────────────────────────────────── */
const PAGE_SIZE = 10
const TIER_COLORS = { High: '#22c55e', Medium: '#f97316', Low: '#ef4444' }

/* ── Custom Tooltip ─────────────────────────────────────────────── */
const ChartTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-detail border border-border rounded-lg px-3 py-2 text-xs shadow-xl">
      {label && <p className="text-t3 mb-1">{label}</p>}
      {payload.map((p, i) => (
        <p key={i} style={{ color: p.color || p.fill }}>
          {p.name}: <span className="font-semibold text-t1">{p.value}</span>
        </p>
      ))}
    </div>
  )
}

/* ── Score Bar ──────────────────────────────────────────────────── */
const ScoreBar = ({ score }) => (
  <div className="flex items-center gap-2">
    <div className="flex-1 h-1.5 bg-border rounded-full overflow-hidden">
      <div
        className="h-full rounded-full transition-all"
        style={{ width: `${score}%`, backgroundColor: scoreColor(score) }}
      />
    </div>
    <span className="text-xs font-semibold w-8 text-right" style={{ color: scoreColor(score) }}>
      {score}
    </span>
  </div>
)

/* ── KPI Card ───────────────────────────────────────────────────── */
const KpiCard = ({ label, count, icon: Icon, color, active, onClick }) => (
  <button
    onClick={onClick}
    className={`card p-5 text-left transition-all duration-150 w-full
      ${active
        ? 'border-green/50 bg-green/5 shadow-lg shadow-green/5'
        : 'hover:border-border-h hover:bg-card-hover'}`}
  >
    <div className="flex items-start justify-between mb-3">
      <div className={`p-2 rounded-lg`} style={{ backgroundColor: color + '15' }}>
        <Icon size={18} style={{ color }} />
      </div>
      {active && (
        <span className="text-xs text-green font-medium px-2 py-0.5 bg-green/10 rounded-full">Active</span>
      )}
    </div>
    <div className="text-3xl font-bold text-t1 mb-0.5">{count}</div>
    <div className="text-sm text-t3">{label}</div>
  </button>
)

/* ── Disposition Chart Data ─────────────────────────────────────── */
function buildDispositionData(accounts) {
  const counts = {}
  accounts.forEach(a => {
    const d = a.disposition || 'Unknown'
    counts[d] = (counts[d] || 0) + 1
  })
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([name, value]) => ({ name: name.length > 22 ? name.slice(0, 22) + '…' : name, value }))
}

/* ── Score Distribution ─────────────────────────────────────────── */
function buildScoreData(accounts) {
  const buckets = [
    { range: '0–20', min: 0, max: 20, count: 0 },
    { range: '21–40', min: 21, max: 40, count: 0 },
    { range: '41–60', min: 41, max: 60, count: 0 },
    { range: '61–80', min: 61, max: 80, count: 0 },
    { range: '81–100', min: 81, max: 100, count: 0 },
  ]
  accounts.forEach(a => {
    const b = buckets.find(b => a.propensity_score >= b.min && a.propensity_score <= b.max)
    if (b) b.count++
  })
  return buckets
}

/* ── Dashboard ──────────────────────────────────────────────────── */
export default function Dashboard() {
  const data = useData()
  const navigate = useNavigate()
  const [tierFilter, setTierFilter] = useState('All')
  const [search, setSearch] = useState('')
  const [sortField, setSortField] = useState('rank')
  const [sortDir, setSortDir] = useState('asc')
  const [page, setPage] = useState(1)

  const accounts = data?.accounts || []
  const meta = {
    total: accounts.length,
    high: accounts.filter(a => a.tier === 'High').length,
    medium: accounts.filter(a => a.tier === 'Medium').length,
    low: accounts.filter(a => a.tier === 'Low').length,
  }

  /* ── Filtered + sorted ── */
  const filtered = useMemo(() => {
    let list = [...accounts]
    if (tierFilter !== 'All') list = list.filter(a => a.tier === tierFilter)
    if (search.trim()) {
      const q = search.toLowerCase()
      list = list.filter(a =>
        a.name?.toLowerCase().includes(q) ||
        a.loan_number?.toLowerCase().includes(q) ||
        a.city?.toLowerCase().includes(q)
      )
    }
    list.sort((a, b) => {
      let av = a[sortField], bv = b[sortField]
      if (av == null) return 1
      if (bv == null) return -1
      if (typeof av === 'string') av = av.toLowerCase()
      if (typeof bv === 'string') bv = bv.toLowerCase()
      return sortDir === 'asc' ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1)
    })
    return list
  }, [accounts, tierFilter, search, sortField, sortDir])

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)
  const paged = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  const toggleSort = (field) => {
    if (sortField === field) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortField(field); setSortDir('asc') }
  }

  const handleTierFilter = (tier) => {
    setTierFilter(tier)
    setPage(1)
  }

  /* ── Chart data ── */
  const pieData = [
    { name: 'High', value: meta.high },
    { name: 'Medium', value: meta.medium },
    { name: 'Low', value: meta.low },
  ]
  const scoreData = buildScoreData(accounts)
  const dispData = buildDispositionData(accounts)

  return (
    <div className="min-h-screen bg-bg">
      <Header />
      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">

        {/* ── Meta bar ── */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-t1">Recovery Intelligence</h1>
            <p className="text-t3 text-sm mt-0.5">
              {accounts.length} accounts analysed ·{' '}
              <span className="text-t4 text-xs">
                {data?.generated_at
                  ? new Date(data.generated_at).toLocaleString('en-IN', {
                      day: '2-digit', month: 'short', year: 'numeric',
                      hour: '2-digit', minute: '2-digit',
                    })
                  : ''}
              </span>
            </p>
          </div>
        </div>

        {/* ── KPI cards ── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <KpiCard
            label="All Accounts"
            count={meta.total}
            icon={Users}
            color="#818cf8"
            active={tierFilter === 'All'}
            onClick={() => handleTierFilter('All')}
          />
          <KpiCard
            label="High Propensity"
            count={meta.high}
            icon={CheckCircle2}
            color="#22c55e"
            active={tierFilter === 'High'}
            onClick={() => handleTierFilter('High')}
          />
          <KpiCard
            label="Medium Propensity"
            count={meta.medium}
            icon={TrendingUp}
            color="#f97316"
            active={tierFilter === 'Medium'}
            onClick={() => handleTierFilter('Medium')}
          />
          <KpiCard
            label="Low Propensity"
            count={meta.low}
            icon={AlertTriangle}
            color="#ef4444"
            active={tierFilter === 'Low'}
            onClick={() => handleTierFilter('Low')}
          />
        </div>

        {/* ── Charts ── */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Tier Donut */}
          <div className="card p-5">
            <h3 className="text-sm font-semibold text-t2 mb-4">Tier Distribution</h3>
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  innerRadius={55}
                  outerRadius={80}
                  paddingAngle={3}
                  dataKey="value"
                >
                  {pieData.map((entry) => (
                    <Cell key={entry.name} fill={TIER_COLORS[entry.name]} />
                  ))}
                </Pie>
                <Tooltip content={<ChartTooltip />} />
              </PieChart>
            </ResponsiveContainer>
            <div className="flex justify-center gap-4 mt-1">
              {pieData.map(e => (
                <div key={e.name} className="flex items-center gap-1.5 text-xs text-t3">
                  <div className="w-2 h-2 rounded-full" style={{ backgroundColor: TIER_COLORS[e.name] }} />
                  {e.name} ({e.value})
                </div>
              ))}
            </div>
          </div>

          {/* Score Distribution */}
          <div className="card p-5">
            <h3 className="text-sm font-semibold text-t2 mb-4">Score Distribution</h3>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={scoreData} barSize={28}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a2d35" vertical={false} />
                <XAxis dataKey="range" tick={{ fill: '#9ca3af', fontSize: 11 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} axisLine={false} tickLine={false} />
                <Tooltip content={<ChartTooltip />} cursor={{ fill: '#1f2230' }} />
                <Bar dataKey="count" name="Accounts" radius={[4, 4, 0, 0]}>
                  {scoreData.map((entry, i) => (
                    <Cell key={i} fill={scoreColor((entry.min + entry.max) / 2)} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Disposition Breakdown */}
          <div className="card p-5">
            <h3 className="text-sm font-semibold text-t2 mb-4">Top Dispositions</h3>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={dispData} layout="vertical" barSize={14}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a2d35" horizontal={false} />
                <XAxis type="number" tick={{ fill: '#9ca3af', fontSize: 10 }} axisLine={false} tickLine={false} />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={110}
                  tick={{ fill: '#9ca3af', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip content={<ChartTooltip />} cursor={{ fill: '#1f2230' }} />
                <Bar dataKey="value" name="Count" fill="#818cf8" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* ── Filter bar ── */}
        <div className="card p-4">
          <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center">
            <div className="relative flex-1">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-t4" />
              <input
                type="text"
                placeholder="Search by name, loan number or city…"
                value={search}
                onChange={e => { setSearch(e.target.value); setPage(1) }}
                className="w-full bg-detail border border-border rounded-lg pl-8 pr-3 py-2 text-sm text-t1
                           placeholder:text-t4 focus:outline-none focus:border-green/50 transition-colors"
              />
            </div>
            <div className="flex items-center gap-2 text-xs text-t3">
              <span>{filtered.length} results</span>
              {tierFilter !== 'All' && (
                <button
                  onClick={() => handleTierFilter('All')}
                  className="px-2 py-0.5 bg-border rounded-full hover:bg-border-h transition-colors"
                >
                  Clear filter
                </button>
              )}
            </div>
          </div>
        </div>

        {/* ── Table ── */}
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-detail">
                  {[
                    { label: '#', field: 'rank', w: 'w-10' },
                    { label: 'Customer', field: 'name', w: 'min-w-[140px]' },
                    { label: 'Loan #', field: 'loan_number', w: 'w-32' },
                    { label: 'DPD Bucket', field: 'dpd_bucket', w: 'min-w-[130px]' },
                    { label: 'Outstanding', field: 'principal_outstanding', w: 'w-32' },
                    { label: 'Score', field: 'propensity_score', w: 'w-40' },
                    { label: 'Tier', field: 'tier', w: 'w-24' },
                    { label: 'Disposition', field: 'disposition', w: 'min-w-[160px]' },
                    { label: 'Reachable', field: null, w: 'w-28' },
                  ].map(col => (
                    <th
                      key={col.label}
                      onClick={col.field ? () => toggleSort(col.field) : undefined}
                      className={`px-4 py-3 text-left text-xs font-semibold text-t3 uppercase tracking-wider
                        ${col.field ? 'cursor-pointer hover:text-t1 select-none' : ''} ${col.w}`}
                    >
                      <span className="flex items-center gap-1">
                        {col.label}
                        {col.field && <ArrowUpDown size={11} className="opacity-40" />}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {paged.map((account, idx) => {
                  const waiver = waiverInfo(account.dpd_bucket)
                  return (
                    <tr
                      key={account.loan_number}
                      onClick={() => navigate(`/account/${account.loan_number}`)}
                      className="border-b border-border/50 hover:bg-card-hover cursor-pointer transition-colors group"
                    >
                      {/* Rank */}
                      <td className="px-4 py-3 text-t4 font-mono text-xs">{account.rank}</td>

                      {/* Name */}
                      <td className="px-4 py-3">
                        <div className="font-medium text-t1 group-hover:text-green transition-colors">
                          {account.name}
                        </div>
                        <div className="text-xs text-t4">{account.city}</div>
                      </td>

                      {/* Loan # */}
                      <td className="px-4 py-3 font-mono text-xs text-t3">{account.loan_number}</td>

                      {/* DPD */}
                      <td className="px-4 py-3">
                        <div className="text-xs text-t2">{dpdShort(account.dpd_bucket)}</div>
                        {waiver && (
                          <div className="text-xs text-green mt-0.5">{waiver}</div>
                        )}
                      </td>

                      {/* Outstanding */}
                      <td className="px-4 py-3 text-t2 font-medium">
                        {fmtCur(account.principal_outstanding || account.total_amount_pending)}
                      </td>

                      {/* Score */}
                      <td className="px-4 py-3 w-40">
                        <ScoreBar score={account.propensity_score} />
                      </td>

                      {/* Tier */}
                      <td className="px-4 py-3">
                        <span className={`badge border ${tierBg(account.tier)}`}>
                          <span
                            className="w-1.5 h-1.5 rounded-full"
                            style={{ backgroundColor: tierDot(account.tier) }}
                          />
                          {account.tier}
                        </span>
                      </td>

                      {/* Disposition */}
                      <td className="px-4 py-3 text-xs text-t2 max-w-[180px]">
                        <div className="flex items-center gap-1.5">
                          <span className="truncate">{account.disposition || '—'}</span>
                          {account.recording_mismatch && (
                            <span title="Recording mismatch — score based on DB disposition">
                              <AlertTriangle size={11} className="text-orange shrink-0" />
                            </span>
                          )}
                          {account.recording_skipped_short_duration && (
                            <span title="Recording skipped — call under 20 seconds">
                              <AlertTriangle size={11} className="text-t4 shrink-0" />
                            </span>
                          )}
                        </div>
                      </td>

                      {/* Reachable */}
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <Phone
                            size={13}
                            className={account.call_reachable ? 'text-green' : 'text-t4'}
                          />
                          <MessageSquare
                            size={13}
                            className={account.whatsapp_reachable ? 'text-green' : 'text-t4'}
                          />
                          <Mail
                            size={13}
                            className={account.email ? 'text-blue' : 'text-t4'}
                          />
                        </div>
                      </td>
                    </tr>
                  )
                })}
                {paged.length === 0 && (
                  <tr>
                    <td colSpan={9} className="px-4 py-12 text-center text-t4">
                      No accounts match your filters.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t border-border">
              <span className="text-xs text-t4">
                Showing {((page - 1) * PAGE_SIZE) + 1}–{Math.min(page * PAGE_SIZE, filtered.length)} of {filtered.length}
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="p-1.5 rounded-lg border border-border text-t3 hover:border-border-h hover:text-t1
                             disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft size={14} />
                </button>
                <div className="flex items-center gap-1">
                  {[...Array(totalPages)].map((_, i) => (
                    <button
                      key={i}
                      onClick={() => setPage(i + 1)}
                      className={`w-7 h-7 rounded-lg text-xs font-medium transition-colors
                        ${page === i + 1
                          ? 'bg-green text-bg'
                          : 'text-t3 hover:bg-card-hover hover:text-t1'}`}
                    >
                      {i + 1}
                    </button>
                  ))}
                </div>
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                  className="p-1.5 rounded-lg border border-border text-t3 hover:border-border-h hover:text-t1
                             disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
