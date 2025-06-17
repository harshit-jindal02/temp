import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/upload': 'http://localhost:8000',
      '/clone':  'http://localhost:8000',
      '/instrument': 'http://localhost:8000',
      '/suggestions': 'http://localhost:8000',
    }
  }
})
