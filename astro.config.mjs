import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import keystatic from '@keystatic/astro';
export default defineConfig({site:'https://deepseek.ac',output:'static',vite:{optimizeDeps:{include:['lodash/debounce','lodash/get','lodash/set'] }},integrations:[...(process.env.NODE_ENV !== 'production' ? [react(),keystatic()] : [])]});
