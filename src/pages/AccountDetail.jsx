import React, { useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  RadialBarChart, RadialBar, ResponsiveContainer,
  BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid, Cell,
} from 'recharts'
import {
  Phone, MessageSquare, Mail, User, MapPin, Briefcase,
  Calendar, TrendingUp, AlertCircle, CheckCircle2, Clock,
  CreditCard, Activity, PhoneCall, ChevronRight,
} from 'lucide-react'
import Header from '../components/Header'
import { useData } from '../App'
import {
  fmtCur, fmtDate, fmtDateTime, tierBg, tierDot, sentimentColor, scoreColor, waiverInfo,
} from '../lib/utils'
import {
  recoveryNarrative, recommendedAction, engagementTag, barrierAdvice, dpdRiskLabel, scoreGrade,
} from '../lib/intelligence'

/* ── Helpers ────────────────────────────────────────────────────── */
const Field = ({ label, value, mono, highlight }) => (
  <div className="flex flex-col gap-0.5">
    <span className="text-xs text-t4 uppercase tracking-wide">{label}</span>
    <span className={`text-sm ${highlight ? 'text-green font-semibold' : 'text-t1'} ${mono ? 'font-mono' : ''}`}>
      {value || '—'}
    </span>
  </div>
)

const COLOR_MAP = {
  green:  { bg: 'bg-green/10',  text: 'text-green' },
  blue:   { bg: 'bg-blue/10',   text: 'text-blue' },
  indigo: { bg: 'bg-indigo/10', text: 'text-indigo' },
  purple: { bg: 'bg-purple/10', text: 'text-purple' },
  orange: { bg: 'bg-orange/10', text: 'text-orange' },
  red:    { bg: 'bg-red/10',    text: 'text-red' },
}

const ContactRow = ({ icon: Icon, label, value, color = 'green' }) => {
  if (!value) return null
  const c = COLOR_MAP[color] || COLOR_MAP.green
  return (
    <div className="flex items-center gap-3 py-2.5 border-b border-border/50 last:border-0">
      <div className={`p-1.5 rounded-lg ${c.bg}`}>
        <Icon size={14} className={c.text} />
      </div>
      <div>
        <div className="text-xs text-t4">{label}</div>
        <div className="text-sm text-t1 font-medium">{value}</div>
      </div>
    </div>
  )
}

const ScorePill = ({ label, value, max }) => {
  const pct = max ? (value / max) * 100 : 0
  return (
    <div className="flex items-center gap-3">
      <div className="text-xs text-t3 w-36 shrink-0">{label}</div>
      <div className="flex-1 h-2 bg-border rounded-full overflow-hidden">
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, backgroundColor: scoreColor(pct) }}
        />
      </div>
      <div className="text-xs font-semibold text-t1 w-16 text-right">
        {value?.toFixed(1)} / {max}
      </div>
    </div>
  )
}

const SCORE_BREAKDOWN_MAXES = {
  disposition_score: 30,
  commitment_score: 20,
  engagement_score: 15,
  sentiment_score: 10,
  duration_score: 5,
  history_score: 10,
  dpd_score: 5,
  bonus_points: 10,
}
const SCORE_LABELS = {
  disposition_score: 'Disposition',
  commitment_score: 'Commitment',
  engagement_score: 'Engagement',
  sentiment_score: 'Sentiment',
  duration_score: 'Call Duration',
  history_score: 'History',
  dpd_score: 'DPD',
  bonus_points: 'Bonus',
}

const ChartTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-detail border border-border rounded-lg px-3 py-2 text-xs shadow-xl">
      {label && <p className="text-t3 mb-1">{label}</p>}
      {payload.map((p, i) => (
        <p key={i} style={{ color: p.fill || '#fff' }}>
          {p.name}: <span className="font-semibold">{p.value}</span>
        </p>
      ))}
    </div>
  )
}

/* ── Tabs ───────────────────────────────────────────────────────── */
const TABS = ['Contact & Loan', 'AI Analysis', 'Call History', 'Payment Info']

/* ── Main Component ─────────────────────────────────────────────── */
export default function AccountDetail() {
  const { loanNumber } = useParams()
  const data = useData()
  const [activeTab, setActiveTab] = useState(0)

  const account = data?.accounts?.find(a => a.loan_number === loanNumber)

  if (!account) return (
    <div className="min-h-screen bg-bg flex items-center justify-center text-t3">
      Account not found.
    </div>
  )

  const grade = scoreGrade(account.propensity_score)
  const riskLabel = dpdRiskLabel(account.dpd_of_customer)
  const narrative = recoveryNarrative(account)
  const action = recommendedAction(account)
  const engagement = engagementTag(account)
  const barrierTip = barrierAdvice(account.barrier_type)
  const waiver = waiverInfo(account.dpd_bucket)

  const scoreBreakdownData = account.score_breakdown
    ? Object.entries(account.score_breakdown)
        .filter(([k]) => SCORE_BREAKDOWN_MAXES[k])
        .map(([k, v]) => ({
          name: SCORE_LABELS[k] || k,
          value: Number(v) || 0,
          max: SCORE_BREAKDOWN_MAXES[k],
          fill: scoreColor((Number(v) / SCORE_BREAKDOWN_MAXES[k]) * 100),
        }))
    : []

  return (
    <div className="min-h-screen bg-bg">
      <Header account={account} />

      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">

        {/* ── Hero Card ── */}
        <div className="card p-6">
          <div className="flex flex-col lg:flex-row lg:items-start gap-6">
            {/* Left: identity */}
            <div className="flex-1">
              <div className="flex items-start gap-4">
                <div className="w-12 h-12 rounded-xl bg-indigo/10 border border-indigo/20 flex items-center justify-center shrink-0">
                  <User size={20} className="text-indigo" />
                </div>
                <div>
                  <h1 className="text-2xl font-bold text-t1">{account.name}</h1>
                  <div className="flex flex-wrap items-center gap-2 mt-1.5">
                    <span className={`badge border ${tierBg(account.tier)}`}>
                      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: tierDot(account.tier) }} />
                      {account.tier} Propensity
                    </span>
                    <span className={`badge border ${sentimentColor(account.sentiment) === 'text-green'
                      ? 'bg-green/10 text-green border-green/20'
                      : sentimentColor(account.sentiment) === 'text-blue'
                        ? 'bg-blue/10 text-blue border-blue/20'
                        : 'bg-red/10 text-red border-red/20'}`}>
                      {account.sentiment}
                    </span>
                    {waiver && (
                      <span className="badge bg-green/10 text-green border border-green/20">{waiver}</span>
                    )}
                  </div>
                  {account.city && (
                    <div className="flex items-center gap-1 mt-2 text-xs text-t4">
                      <MapPin size={11} />
                      {account.city}
                    </div>
                  )}
                </div>
              </div>

              {/* Recovery Narrative */}
              <div className="mt-4 p-3 bg-detail rounded-lg border border-border">
                <div className="text-xs text-t4 uppercase tracking-wide mb-1">Recovery Narrative</div>
                <p className="text-sm text-t2 leading-relaxed">{narrative}</p>
              </div>
            </div>

            {/* Right: score + action */}
            <div className="flex flex-col gap-4 lg:w-64">
              {/* Score gauge */}
              <div className="card p-4 border-border text-center">
                <div className="text-xs text-t4 uppercase tracking-wide mb-2">Propensity Score</div>
                <div className="flex items-center justify-center gap-3">
                  <div
                    className="text-5xl font-black"
                    style={{ color: grade.color }}
                  >
                    {account.propensity_score}
                  </div>
                  <div className="text-left">
                    <div className="text-2xl font-bold" style={{ color: grade.color }}>{grade.grade}</div>
                    <div className="text-xs text-t3">{grade.label}</div>
                  </div>
                </div>
                <div className="mt-3 w-full h-2 bg-border rounded-full overflow-hidden">
                  <div
                    className="h-full rounded-full transition-all"
                    style={{ width: `${account.propensity_score}%`, backgroundColor: grade.color }}
                  />
                </div>
                <div className="flex justify-between text-xs text-t4 mt-1">
                  <span>0</span>
                  <span>Rank #{account.rank}</span>
                  <span>100</span>
                </div>
              </div>

              {/* Recommended action */}
              <div className="card p-4 border-border">
                <div className="text-xs text-t4 uppercase tracking-wide mb-2">Recommended Action</div>
                <div className={`flex items-center gap-2 text-sm font-medium
                  ${action.priority === 'high' ? 'text-green' : action.priority === 'medium' ? 'text-orange' : 'text-blue'}`}>
                  <ChevronRight size={14} />
                  {action.action}
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* ── Tabs ── */}
        <div className="flex gap-1 bg-detail p-1 rounded-xl border border-border w-fit">
          {TABS.map((tab, i) => (
            <button
              key={tab}
              onClick={() => setActiveTab(i)}
              className={`tab-btn ${activeTab === i ? 'tab-active' : 'tab-inactive'}`}
            >
              {tab}
            </button>
          ))}
        </div>

        {/* ── Tab 0: Contact & Loan ── */}
        {activeTab === 0 && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            {/* Contact Info */}
            <div className="card p-5">
              <h3 className="text-sm font-semibold text-t1 mb-4 flex items-center gap-2">
                <Phone size={15} className="text-green" />
                Contact Details
              </h3>
              <div className="divide-y divide-border/40">
                <ContactRow icon={Phone} label="Primary Contact" value={account.primary_contact_number} color="green" />
                <ContactRow icon={Phone} label="Secondary Contact" value={account.secondary_contact_number} color="blue" />
                <ContactRow icon={MessageSquare} label="WhatsApp" value={account.whatsapp_contact_number} color="green" />
                <ContactRow icon={Mail} label="Email" value={account.email} color="blue" />
                {account.co_applicant_name && (
                  <ContactRow icon={User} label={`Co-Applicant (${account.co_applicant_name})`} value={account.co_applicant_contact} color="indigo" />
                )}
                <ContactRow icon={Phone} label="Reference 1" value={account.reference_contact_1} color="purple" />
                <ContactRow icon={Phone} label="Reference 2" value={account.reference_contact_2} color="purple" />
              </div>

              {/* Reachability */}
              <div className="mt-4 pt-4 border-t border-border">
                <div className="text-xs text-t4 uppercase tracking-wide mb-3">Reachability</div>
                <div className="grid grid-cols-3 gap-2">
                  {[
                    { label: 'Call', value: account.call_reachable, icon: Phone },
                    { label: 'WhatsApp', value: account.whatsapp_reachable, icon: MessageSquare },
                    { label: 'SMS', value: account.msg_reachable, icon: Mail },
                  ].map(({ label, value, icon: Icon }) => (
                    <div
                      key={label}
                      className={`flex flex-col items-center gap-1.5 p-3 rounded-lg border
                        ${value ? 'border-green/20 bg-green/5' : 'border-border bg-detail'}`}
                    >
                      <Icon size={14} className={value ? 'text-green' : 'text-t4'} />
                      <span className="text-xs text-t3">{label}</span>
                      <span className={`text-xs font-medium ${value ? 'text-green' : 'text-t4'}`}>
                        {value ? 'Yes' : 'No'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* Loan Info */}
            <div className="card p-5">
              <h3 className="text-sm font-semibold text-t1 mb-4 flex items-center gap-2">
                <CreditCard size={15} className="text-indigo" />
                Loan Details
              </h3>
              <div className="grid grid-cols-2 gap-x-6 gap-y-4">
                <Field label="Loan Number" value={account.loan_number} mono />
                <Field label="Lender" value={account.lender} />
                <Field label="Loan Amount" value={fmtCur(account.loan_amount)} highlight />
                <Field label="EMI Amount" value={fmtCur(account.emi_amount)} />
                <Field label="Principal Outstanding" value={fmtCur(account.principal_outstanding)} highlight />
                <Field label="Bounce Amount" value={fmtCur(account.bounce_amount)} />
                <Field label="Tenure" value={account.tenure ? `${account.tenure} months` : null} />
                <Field label="DPD Bucket" value={account.dpd_bucket} />
                <Field label="Disbursal Date" value={fmtDate(account.disbursal_date)} />
                <Field label="EMI Date" value={fmtDate(account.emi_date)} />
                <Field label="Risk Category" value={account.risk} />
                <Field label="Occupation" value={account.occupation} />
              </div>
              {account.address && (
                <div className="mt-4 pt-4 border-t border-border">
                  <div className="text-xs text-t4 uppercase tracking-wide mb-1">Address</div>
                  <div className="flex items-start gap-2 text-sm text-t2">
                    <MapPin size={13} className="text-t4 mt-0.5 shrink-0" />
                    <span className="leading-relaxed">{account.address}</span>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ── Tab 1: AI Analysis ── */}
        {activeTab === 1 && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            {/* AI Fields */}
            <div className="space-y-5">
              {/* Call Summary */}
              <div className="card p-5">
                <h3 className="text-sm font-semibold text-t1 mb-3 flex items-center gap-2">
                  <Activity size={15} className="text-indigo" />
                  AI Call Summary
                </h3>
                <p className="text-sm text-t2 leading-relaxed">{account.summary || '—'}</p>
              </div>

              {/* Key Reasons */}
              {account.key_reasons?.length > 0 && (
                <div className="card p-5">
                  <h3 className="text-sm font-semibold text-t1 mb-3 flex items-center gap-2">
                    <CheckCircle2 size={15} className="text-green" />
                    Key Score Drivers
                  </h3>
                  <ul className="space-y-2">
                    {account.key_reasons.map((r, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm text-t2">
                        <span className="text-green mt-0.5">•</span>
                        {r}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Barrier Advice */}
              {account.barrier_type && account.barrier_type !== 'none' && (
                <div className="card p-5 border-orange/20 bg-orange/5">
                  <h3 className="text-sm font-semibold text-orange mb-2 flex items-center gap-2">
                    <AlertCircle size={15} />
                    Barrier: {account.barrier_type}
                  </h3>
                  <p className="text-sm text-t2 leading-relaxed">{barrierTip}</p>
                </div>
              )}
            </div>

            {/* Score Breakdown */}
            <div className="space-y-5">
              {/* Propensity fields */}
              <div className="card p-5">
                <h3 className="text-sm font-semibold text-t1 mb-4">AI Signal Summary</h3>
                <div className="grid grid-cols-2 gap-x-6 gap-y-4">
                  <Field label="Disposition" value={account.disposition} />
                  <Field label="Commitment Strength" value={account.commitment_strength} />
                  <Field label="Engagement Level" value={account.engagement_level} />
                  <Field label="Barrier Type" value={account.barrier_type} />
                  <Field label="Tone Shift" value={account.tone_shift} />
                  <Field label="Promise Made" value={account.promise_made ? 'Yes' : 'No'} />
                  <Field label="Promise Date" value={fmtDate(account.promise_date)} />
                  <Field label="Amount Discussed" value={account.specific_amount_discussed ? 'Yes' : 'No'} />
                  <Field label="Customer Initiated" value={account.customer_initiated_resolution ? 'Yes' : 'No'} />
                  <div />
                </div>
              </div>

              {/* Score breakdown bars */}
              <div className="card p-5">
                <h3 className="text-sm font-semibold text-t1 mb-4">Score Breakdown</h3>
                <div className="space-y-3">
                  {Object.entries(account.score_breakdown || {})
                    .filter(([k]) => SCORE_BREAKDOWN_MAXES[k])
                    .map(([k, v]) => (
                      <ScorePill
                        key={k}
                        label={SCORE_LABELS[k] || k}
                        value={Number(v)}
                        max={SCORE_BREAKDOWN_MAXES[k]}
                      />
                    ))}
                </div>
                <div className="mt-4 pt-3 border-t border-border flex items-center justify-between">
                  <span className="text-sm text-t3">Total Score</span>
                  <span className="text-lg font-bold" style={{ color: scoreColor(account.propensity_score) }}>
                    {account.propensity_score} / 100
                  </span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ── Tab 2: Call History ── */}
        {activeTab === 2 && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            <div className="card p-5">
              <h3 className="text-sm font-semibold text-t1 mb-4 flex items-center gap-2">
                <PhoneCall size={15} className="text-blue" />
                Call Statistics
              </h3>
              <div className="grid grid-cols-2 gap-x-6 gap-y-4">
                <Field label="Total Calls" value={account.total_calls} />
                <Field label="Total Payments" value={account.total_payments} />
                <Field label="Last Payment Date" value={fmtDate(account.last_payment_date)} />
                <Field label="Analysed At" value={fmtDateTime(account.analysed_at)} />
              </div>
            </div>

            <div className="card p-5">
              <h3 className="text-sm font-semibold text-t1 mb-4 flex items-center gap-2">
                <Clock size={15} className="text-purple" />
                Previous Dispositions
              </h3>
              {account.previous_dispositions?.length > 0 ? (
                <div className="space-y-2">
                  {account.previous_dispositions.map((d, i) => (
                    <div key={i} className="flex items-center gap-3 py-1.5 border-b border-border/40 last:border-0">
                      <span className="text-xs text-t4 w-5 shrink-0">#{i + 1}</span>
                      <span className="text-sm text-t2">{d}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-t4">No previous disposition history.</p>
              )}
            </div>
          </div>
        )}

        {/* ── Tab 3: Payment Info ── */}
        {activeTab === 3 && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            <div className="card p-5">
              <h3 className="text-sm font-semibold text-t1 mb-4 flex items-center gap-2">
                <CreditCard size={15} className="text-green" />
                Payment Status
              </h3>
              <div className="grid grid-cols-2 gap-x-6 gap-y-4">
                <Field label="Payment Status" value={account.payment_status} highlight={account.payment_status === 'Paid'} />
                <Field label="PTP Amount" value={fmtCur(account.ptp_amount)} />
                <Field label="Bounce Amount" value={fmtCur(account.bounce_amount)} />
                <Field label="DPD of Customer" value={account.dpd_of_customer ? `${account.dpd_of_customer} days` : null} />
                <Field label="Previous PTP Date" value={fmtDate(account.previous_ptp_date)} />
                <Field label="Follow-Up Date" value={fmtDateTime(account.follow_up_datetime)} />
              </div>

              {/* Risk label */}
              <div className="mt-4 pt-4 border-t border-border">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-t4 uppercase tracking-wide">DPD Risk Level</span>
                  <span className={
                    riskLabel.color === 'red' ? 'badge border bg-red/10 text-red border-red/20' :
                    riskLabel.color === 'orange' ? 'badge border bg-orange/10 text-orange border-orange/20' :
                    riskLabel.color === 'blue' ? 'badge border bg-blue/10 text-blue border-blue/20' :
                    'badge border bg-border text-t3 border-border'
                  }>
                    {riskLabel.label}
                  </span>
                </div>
              </div>
            </div>

            <div className="card p-5">
              <h3 className="text-sm font-semibold text-t1 mb-4 flex items-center gap-2">
                <TrendingUp size={15} className="text-orange" />
                Late Installment Trend
              </h3>
              {(account.late_installments_3m != null || account.late_installments_6m != null || account.late_installments_12m != null) ? (
                <>
                  <ResponsiveContainer width="100%" height={160}>
                    <BarChart
                      data={[
                        { period: '3M', value: account.late_installments_3m || 0 },
                        { period: '6M', value: account.late_installments_6m || 0 },
                        { period: '12M', value: account.late_installments_12m || 0 },
                      ]}
                      barSize={36}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#2a2d35" vertical={false} />
                      <XAxis dataKey="period" tick={{ fill: '#9ca3af', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fill: '#9ca3af', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <Tooltip content={<ChartTooltip />} cursor={{ fill: '#1f2230' }} />
                      <Bar dataKey="value" name="Late EMIs" radius={[4, 4, 0, 0]} fill="#f97316" />
                    </BarChart>
                  </ResponsiveContainer>
                  <div className="grid grid-cols-3 gap-3 mt-3 text-center">
                    {[
                      { label: 'Last 3M', val: account.late_installments_3m },
                      { label: 'Last 6M', val: account.late_installments_6m },
                      { label: 'Last 12M', val: account.late_installments_12m },
                    ].map(({ label, val }) => (
                      <div key={label} className="p-2 bg-detail rounded-lg border border-border">
                        <div className="text-lg font-bold text-orange">{val ?? '—'}</div>
                        <div className="text-xs text-t4">{label}</div>
                      </div>
                    ))}
                  </div>
                  {account.late_trend_slope != null && (
                    <div className="mt-3 text-xs text-t3">
                      Trend slope: <span className={account.late_trend_slope > 0 ? 'text-red' : 'text-green'}>
                        {account.late_trend_slope > 0 ? '▲' : '▼'} {account.late_trend_slope?.toFixed(2)}
                      </span>
                    </div>
                  )}
                </>
              ) : (
                <p className="text-sm text-t4 mt-2">No late installment data available.</p>
              )}
            </div>
          </div>
        )}

      </div>
    </div>
  )
}
