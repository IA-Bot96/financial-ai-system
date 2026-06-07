/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/renderer/index.html', './src/renderer/src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#0f1115',
        panel: '#171a21',
        panel2: '#1d2129',
        line: '#2a2f3a',
        ink: '#e7ebf0',
        muted: '#9aa4b2',
        accent: '#4f8cff'
      }
    }
  },
  plugins: []
}
