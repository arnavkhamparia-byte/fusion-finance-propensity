/* ── AI Intelligence Layer ─────────────────────────────────────── */

export function recoveryNarrative(a) {
  const parts = []

  // Commitment
  if (a.promise_made && a.promise_date) {
    parts.push(`Customer made a payment promise for ${a.promise_date}.`)
  } else if (a.commitment_strength === 'strong') {
    parts.push('Customer showed strong commitment to resolve the account.')
  } else if (a.commitment_strength === 'moderate') {
    parts.push('Customer expressed moderate willingness to pay.')
  } else if (a.commitment_strength === 'weak') {
    parts.push('Customer showed limited commitment — follow-up required.')
  }

  // Barrier
  if (a.barrier_type === 'financial') {
    parts.push('Cited financial hardship as the primary barrier.')
  } else if (a.barrier_type === 'dispute') {
    parts.push('Has an active dispute that needs resolution before payment.')
  } else if (a.barrier_type === 'temporary') {
    parts.push('Temporary situation — likely to resolve shortly.')
  } else if (a.barrier_type === 'willful') {
    parts.push('Shows signs of willful non-payment — escalation may be needed.')
  }

  // Tone
  if (a.tone_shift === 'improved') {
    parts.push('Tone improved during the call — positive signal.')
  } else if (a.tone_shift === 'deteriorated') {
    parts.push('Tone deteriorated — approach with caution.')
  }

  // Customer initiative
  if (a.customer_initiated_resolution) {
    parts.push('Customer proactively asked about resolution options.')
  }

  if (parts.length === 0) return 'Insufficient signal from last call to generate narrative.'
  return parts.join(' ')
}

export function recommendedAction(a) {
  const disp = (a.disposition || '').toLowerCase()

  if (disp.includes('ptp') || a.promise_made) {
    return { action: 'Follow up on PTP', priority: 'high', icon: 'clock' }
  }
  if (disp.includes('senior manager') || disp.includes('settlement')) {
    return { action: 'Escalate to senior manager', priority: 'high', icon: 'arrow-up-right' }
  }
  if (disp.includes('rescheduled') || disp.includes('callback')) {
    return { action: 'Scheduled callback pending', priority: 'medium', icon: 'calendar' }
  }
  if (disp.includes('financial hardship') || a.barrier_type === 'financial') {
    return { action: 'Offer EMI restructuring / waiver', priority: 'medium', icon: 'banknote' }
  }
  if (disp.includes('dispute') || a.barrier_type === 'dispute') {
    return { action: 'Resolve dispute first', priority: 'medium', icon: 'alert-circle' }
  }
  if (disp.includes('not reachable') || disp.includes('switched off')) {
    return { action: 'Try alternate contact / WhatsApp', priority: 'low', icon: 'phone-missed' }
  }

  return { action: 'Standard follow-up call', priority: 'low', icon: 'phone' }
}

export function engagementTag(a) {
  const level = (a.engagement_level || '').toLowerCase()
  if (level === 'high') return { label: 'Highly Engaged', color: 'green' }
  if (level === 'medium') return { label: 'Moderately Engaged', color: 'blue' }
  if (level === 'low') return { label: 'Low Engagement', color: 'orange' }
  return { label: 'Not Assessed', color: 't4' }
}

export function barrierAdvice(barrier) {
  const map = {
    financial: 'Consider offering a waiver or EMI restructuring. Highlight credit score impact.',
    dispute: 'Resolve the dispute before requesting payment. Loop in the dispute resolution team.',
    temporary: 'Customer situation may change — schedule a follow-up in 3–5 days.',
    willful: 'Escalate to field collections or legal team. Document all interactions.',
    none: 'No specific barrier identified — standard collection approach applies.',
  }
  return map[(barrier || '').toLowerCase()] || 'Assess barrier type in the next call before proceeding.'
}

export function dpdRiskLabel(dpd) {
  if (dpd == null) return { label: 'Unknown', color: 't3' }
  if (dpd <= 30) return { label: 'Early DPD', color: 'blue' }
  if (dpd <= 60) return { label: 'Moderate Risk', color: 'orange' }
  if (dpd <= 90) return { label: 'High Risk', color: 'orange' }
  return { label: 'Critical — NPA Risk', color: 'red' }
}

export function scoreGrade(score) {
  if (score >= 85) return { grade: 'A+', label: 'Very High', color: '#22c55e' }
  if (score >= 70) return { grade: 'A', label: 'High', color: '#22c55e' }
  if (score >= 55) return { grade: 'B', label: 'Medium-High', color: '#84cc16' }
  if (score >= 40) return { grade: 'C', label: 'Medium', color: '#f97316' }
  if (score >= 25) return { grade: 'D', label: 'Low', color: '#f97316' }
  return { grade: 'F', label: 'Very Low', color: '#ef4444' }
}
