import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
    plugins: [tailwindcss(), sveltekit()],
    // The /print preview worker is a module worker (`new Worker(url, { type: 'module' })`).
    worker: { format: 'es' }
});
