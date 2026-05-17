import React from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { BarChart3, ChevronLeft } from 'lucide-react'

export default function Header({ account }) {
  const navigate = useNavigate()
  const location = useLocation()
  const isDetail = location.pathname.startsWith('/account/')

  return (
    <header className="sticky top-0 z-50 bg-sidebar border-b border-border">
      <div className="max-w-7xl mx-auto px-6 h-14 flex items-center justify-between">
        {/* Left */}
        <div className="flex items-center gap-3">
          {isDetail ? (
            <button
              onClick={() => navigate('/')}
              className="flex items-center gap-1.5 text-t3 hover:text-t1 transition-colors text-sm"
            >
              <ChevronLeft size={16} />
              Dashboard
            </button>
          ) : (
            <div className="flex items-center gap-2">
              <BarChart3 size={20} className="text-green" />
              <span className="font-bold text-t1 tracking-tight">Fusion Finance</span>
              <span className="text-t4 text-sm font-normal">/ Propensity</span>
            </div>
          )}
        </div>

        {/* Center: account name on detail page */}
        {isDetail && account && (
          <div className="flex items-center gap-2">
            <span className="font-semibold text-t1">{account.name}</span>
            <span className="text-t4 text-sm">#{account.loan_number}</span>
          </div>
        )}
        {!isDetail && (
          <div className="flex items-center gap-2">
            <BarChart3 size={18} className="text-green" />
            <span className="font-semibold text-t1 tracking-tight hidden sm:block">Propensity Dashboard</span>
          </div>
        )}

        {/* Right */}
        <div className="text-xs text-t4">
          {new Date().toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })}
        </div>
      </div>
    </header>
  )
}
