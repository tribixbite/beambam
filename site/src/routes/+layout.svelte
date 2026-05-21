<script lang="ts">
    import '../app.css';
    import { page } from '$app/stores';
    let { children } = $props();

    // Workshop manual TOC — each link gets a numbered prefix like a drawing index.
    const nav = [
        { num: '01', href: '/', label: 'Overview' },
        { num: '02', href: '/printers', label: 'Compatibility' },
        { num: '03', href: '/cli', label: 'Command index' },
        { num: '04', href: '/docs', label: 'Reference' }
    ];

    const VERSION = 'v1.2.0';
    const REV = '21 May 2026';
</script>

<svelte:head>
    <title>beambam — LAN-first stack for Bambu Lab printers</title>
</svelte:head>

<div class="min-h-screen flex flex-col">
    <!-- Title block: structured like an engineering-drawing header. -->
    <header class="border-b border-[var(--color-hair)]">
        <div class="frame py-[var(--space-lg)] flex items-baseline gap-[var(--space-2xl)]">
            <!-- Wordmark with technical sub-label -->
            <a href="/" class="no-underline shrink-0" style="text-decoration: none;">
                <div class="font-[var(--font-display)] text-3xl leading-none tracking-tighter">
                    <span style="color: var(--color-accent);">beam</span><span style="color: var(--color-ink);">bam</span>
                </div>
                <div class="label mt-1">.boo &middot; {VERSION} &middot; rev {REV}</div>
            </a>

            <!-- Numbered nav, right-aligned -->
            <nav class="ml-auto hidden md:flex items-baseline gap-[var(--space-2xl)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest">
                {#each nav as { num, href, label }}
                    {@const active = $page.url.pathname === href || (href !== '/' && $page.url.pathname.startsWith(href))}
                    <a {href}
                       class="no-underline"
                       style="text-decoration: none; color: {active ? 'var(--color-ink)' : 'var(--color-mute)'};"
                    >
                        <span style="color: var(--color-mute-2);">{num}</span>
                        &nbsp;{label}
                    </a>
                {/each}
                <a href="https://github.com/tribixbite/beambam"
                   class="no-underline"
                   target="_blank" rel="noopener"
                   style="text-decoration: none; color: var(--color-mute);">
                    <span style="color: var(--color-mute-2);">&rarr;</span>&nbsp;GitHub
                </a>
            </nav>
        </div>
    </header>

    <main class="flex-1">
        {@render children()}
    </main>

    <!-- Drawing legend: footer is small fixed metadata, not flexible -->
    <footer class="border-t border-[var(--color-hair)] mt-[var(--space-6xl)]">
        <div class="frame py-[var(--space-xl)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest text-[var(--color-mute)] flex flex-wrap gap-x-[var(--space-2xl)] gap-y-[var(--space-sm)] items-baseline">
            <span><span style="color: var(--color-mute-2);">PKG</span>&nbsp;beambam {VERSION}</span>
            <span><span style="color: var(--color-mute-2);">LIC</span>&nbsp;MIT &middot; BambuStudio fork AGPL-3.0</span>
            <span class="ml-auto">
                <a href="https://github.com/tribixbite/beambam" class="no-underline" style="text-decoration: none; color: inherit;">github</a>
                &nbsp;&middot;&nbsp;
                <a href="https://pypi.org/project/beambam/" class="no-underline" style="text-decoration: none; color: inherit;">pypi</a>
                &nbsp;&middot;&nbsp;
                <a href="https://github.com/tribixbite/beambam/issues" class="no-underline" style="text-decoration: none; color: inherit;">issues</a>
            </span>
        </div>
    </footer>
</div>
