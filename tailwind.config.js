/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg:           '#0f1117',
        card:         '#1a1d24',
        'card-hover': '#1f2230',
        detail:       '#13161e',
        sidebar:      '#141720',
        border:       '#2a2d35',
        'border-h':   '#3a3d45',
        green:        '#22c55e',
        orange:       '#f97316',
        red:          '#ef4444',
        blue:         '#3b82f6',
        purple:       '#8b5cf6',
        indigo:       '#818cf8',
        t1:           '#ffffff',
        t2:           '#d1d5db',
        t3:           '#9ca3af',
        t4:           '#6b7280',
      },
      fontFamily: { sans: ['Inter', 'sans-serif'] },
      animation: {
        'pulse-slow': 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
}
