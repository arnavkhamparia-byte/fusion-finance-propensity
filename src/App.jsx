import React, { createContext, useContext, useEffect, useState } from 'react'
import { Routes, Route } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import AccountDetail from './pages/AccountDetail'

/* ── Data Context ──────────────────────────────────────────────── */
export const DataContext = createContext(null)
export const useData = () => useContext(DataContext)

/* ── Loading Skeleton ──────────────────────────────────────────── */
function LoadingSkeleton() {
  return (
    <div className="min-h-screen bg-bg flex flex-col">
      <div className="h-14 bg-sidebar border-b border-border" />
      <div className="p-6 max-w-7xl mx-auto w-full space-y-6">
        <div className="grid grid-cols-4 gap-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-28 bg-card rounded-xl border border-border animate-pulse" />
          ))}
        </div>
        <div className="grid grid-cols-3 gap-4">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-60 bg-card rounded-xl border border-border animate-pulse" />
          ))}
        </div>
        <div className="h-96 bg-card rounded-xl border border-border animate-pulse" />
      </div>
    </div>
  )
}

/* ── App ───────────────────────────────────────────────────────── */
export default function App() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    const base = import.meta.env.BASE_URL || '/'
    fetch(`${base}data/propensity_results.json`)
      .then(r => { if (!r.ok) throw new Error(r.statusText); return r.json() })
      .then(setData)
      .catch(e => setError(e.message))
  }, [])

  if (error) return (
    <div className="min-h-screen bg-bg flex items-center justify-center text-red">
      Error loading data: {error}
    </div>
  )
  if (!data) return <LoadingSkeleton />

  return (
    <DataContext.Provider value={data}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/account/:loanNumber" element={<AccountDetail />} />
      </Routes>
    </DataContext.Provider>
  )
}
