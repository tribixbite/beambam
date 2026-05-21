<script lang="ts">
    import '../app.css';
    let { children } = $props();
</script>

<svelte:head>
    <title>beambam — LAN-first stack for Bambu Lab printers</title>
</svelte:head>

<div class="min-h-screen flex flex-col">
    <header class="border-b border-steel-700 bg-steel-900/80 backdrop-blur sticky top-0 z-10">
        <div class="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
            <a href="/" class="flex items-center gap-2 text-flame-400 hover:text-flame-300 no-underline">
                <span class="text-2xl font-bold tracking-tight">beambam</span>
                <span class="text-xs uppercase tracking-widest text-stone-500 hidden sm:inline">.boo</span>
            </a>
            <nav class="flex items-center gap-6 text-sm text-stone-400">
                <a href="/docs" class="hover:text-stone-100">Docs</a>
                <a href="/cli" class="hover:text-stone-100">CLI</a>
                <a href="/printers" class="hover:text-stone-100">Compatibility</a>
                <a href="https://github.com/tribixbite/beambam" class="hover:text-stone-100" target="_blank" rel="noopener">GitHub</a>
                <a href="https://pypi.org/project/beambam/" class="hover:text-stone-100" target="_blank" rel="noopener">PyPI</a>
            </nav>
        </div>
    </header>

    <main class="flex-1">
        {@render children()}
    </main>

    <footer class="border-t border-steel-700 mt-16 py-8 text-sm text-stone-500">
        <div class="max-w-6xl mx-auto px-6 flex flex-col sm:flex-row justify-between gap-3">
            <span>MIT licensed · BambuStudio fork AGPL-3.0</span>
            <span>
                <a href="https://github.com/tribixbite/beambam">source</a> ·
                <a href="https://github.com/tribixbite/beambam/issues">issues</a> ·
                <a href="https://pypi.org/project/beambam/">pypi</a>
            </span>
        </div>
    </footer>
</div>
