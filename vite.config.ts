/* SPDX-License-Identifier: Apache-2.0 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'openfocus/static/dist',
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      input: {
        'agent-space': 'frontend/src/entries/agent-space.tsx',
        'inspiration-space': 'frontend/src/entries/inspiration-space.tsx',
      },
      output: {
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
