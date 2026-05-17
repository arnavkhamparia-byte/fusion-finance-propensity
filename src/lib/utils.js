/* ── Formatting ─────────────────────────────────────────────────── */

export const fmtCur = (v) =>
  v == null ? '—' : '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })

export const fmtNum = (v) =>
  v == null ? '—' : Number(v).toLocaleString('en-IN')

export const fmtDate = (v) => {
  if (!v) return '—'
  try {
    const d = new Date(v)
    return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
  } catch { return v }
}

export const fmtDateTime = (v) => {
  if (!v) return '—'
  try {
    const d = new Date(v)
    return d.toLocaleString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return v }
}

export const fmtPct = (v) =>
  v == null ? '—' : `${Number(v).toFixed(1)}%`

/* ── Date helpers ───────────────────────────────────────────────── */

export const daysDiff = (dateStr) => {
  if (!dateStr) return null
  const diff = Math.round((new Date(dateStr) - new Date()) / 86400000)
  return diff
}

/* ── Tier helpers ───────────────────────────────────────────────── */

export const tierColor = (tier) => {
  if (!tier) return 'text-t3'
  const t = tier.toLowerCase()
  if (t === 'high') return 'text-green'
  if (t === 'medium') return 'text-orange'
  return 'text-red'
}

export const tierBg = (tier) => {
  if (!tier) return 'bg-t4/10'
  const t = tier.toLowerCase()
  if (t === 'high') return 'bg-green/10 text-green border-green/20'
  if (t === 'medium') return 'bg-orange/10 text-orange border-orange/20'
  return 'bg-red/10 text-red border-red/20'
}

export const tierDot = (tier) => {
  if (!tier) return '#6b7280'
  const t = tier.toLowerCase()
  if (t === 'high') return '#22c55e'
  if (t === 'medium') return '#f97316'
  return '#ef4444'
}

/* ── Sentiment helpers ──────────────────────────────────────────── */

export const sentimentColor = (s) => {
  if (!s) return 'text-t3'
  const v = s.toLowerCase()
  if (v === 'positive') return 'text-green'
  if (v === 'neutral') return 'text-blue'
  return 'text-red'
}

/* ── Score bar color ────────────────────────────────────────────── */

export const scoreColor = (score) => {
  if (score >= 75) return '#22c55e'
  if (score >= 50) return '#f97316'
  return '#ef4444'
}

/* ── DPD bucket short label ─────────────────────────────────────── */

export const dpdShort = (bucket) => {
  if (!bucket) return '—'
  const m = bucket.match(/^(\d+)\s*-\s*(\d+)/)
  if (m) return `${m[1]}-${m[2]} DPD`
  return bucket.split('-')[0].trim()
}

/* ── Waiver info from dpd_bucket ────────────────────────────────── */

export const waiverInfo = (bucket) => {
  if (!bucket) return null
  const m = bucket.match(/(\d+)%\s*Waiver/i)
  return m ? `${m[1]}% waiver available` : null
}
