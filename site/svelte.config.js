import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
    preprocess: vitePreprocess(),
    kit: {
        adapter: adapter({
            // GitHub Pages serves index.html for unknown paths via the
            // 404.html SPA-fallback trick. Pre-render every known route
            // and let the fallback catch anything else.
            pages: 'build',
            assets: 'build',
            fallback: '404.html',
            strict: true
        }),
        paths: {
            // beambam.boo is the canonical domain (set via static/CNAME on
            // GitHub Pages). No subpath.
            base: ''
        },
        prerender: {
            // Crawl from / + give every route an explicit entry so the
            // SPA fallback isn't the only HTML in the output.
            entries: ['/', '/docs', '/cli', '/printers'],
            handleHttpError: ({ path, message }) => {
                console.warn(`prerender warn @ ${path}: ${message}`);
            }
        }
    }
};

export default config;
