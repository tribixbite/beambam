<script lang="ts">
    import '../app.css';
    import { page } from '$app/stores';
    let { children } = $props();

    let mobileNavOpen = $state(false);

    const nav = [
        { num: '01', href: '/', label: 'Overview' },
        { num: '02', href: '/printers', label: 'Compatibility' },
        { num: '03', href: '/cli', label: 'Command index' },
        { num: '04', href: '/docs', label: 'Reference' }
    ];

    const VERSION = 'v1.2.0';
    const REV = '21 May 2026';

    function isActive(href: string, current: string): boolean {
        if (href === '/') return current === '/';
        return current.startsWith(href);
    }
</script>

<svelte:head>
    <title>beambam — LAN-first stack for Bambu Lab printers</title>
</svelte:head>

<div class="min-h-screen flex flex-col">
    <!-- Header: compact 56–64px sticky bar, wordmark + nav. Mobile collapses to hamburger. -->
    <header class="sticky top-0 z-30 bg-[color-mix(in_oklab,var(--color-surface)_88%,transparent)] backdrop-blur-md" style="border-bottom: 1px solid var(--color-hair);">
        <div class="frame flex items-center gap-[var(--space-lg)]" style="height: 56px;">
            <a href="/" class="no-underline shrink-0 flex items-baseline gap-[var(--space-sm)]" style="text-decoration: none;">
                <span class="font-[var(--font-display)] leading-none tracking-tighter text-[1.5rem] sm:text-[1.75rem]">
                    <span style="color: var(--color-accent);">beam</span><span style="color: var(--color-ink);">bam</span>
                </span>
                <span class="label hidden sm:inline">.boo · {VERSION}</span>
            </a>

            <!-- Desktop nav -->
            <nav class="ml-auto hidden md:flex items-baseline gap-[var(--space-2xl)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest">
                {#each nav as { num, href, label }}
                    {@const active = isActive(href, $page.url.pathname)}
                    <a {href}
                       class="no-underline transition-colors"
                       style="text-decoration: none; color: {active ? 'var(--color-ink)' : 'var(--color-mute)'};"
                    >
                        <span style="color: var(--color-mute-2);">{num}</span>
                        &nbsp;{label}
                    </a>
                {/each}
                <a href="https://github.com/tribixbite/beambam"
                   target="_blank" rel="noopener"
                   class="no-underline transition-colors"
                   style="text-decoration: none; color: var(--color-mute);">
                    <span style="color: var(--color-mute-2);">↗</span>&nbsp;GitHub
                </a>
            </nav>

            <!-- Mobile nav trigger -->
            <button type="button"
                    onclick={() => (mobileNavOpen = !mobileNavOpen)}
                    aria-label={mobileNavOpen ? 'Close menu' : 'Open menu'}
                    aria-expanded={mobileNavOpen}
                    class="md:hidden ml-auto flex items-center justify-center"
                    style="
                        background: transparent;
                        border: 1px solid var(--color-hair);
                        color: var(--color-ink);
                        font-family: var(--font-mono);
                        font-size: var(--text-xs);
                        letter-spacing: 0.15em;
                        text-transform: uppercase;
                        padding: 0.4rem 0.75rem;
                        min-height: 36px;
                        cursor: pointer;
                    ">
                {mobileNavOpen ? 'CLOSE' : 'MENU'}
            </button>
        </div>

        <!-- Mobile nav drawer -->
        {#if mobileNavOpen}
            <div class="md:hidden" style="border-top: 1px solid var(--color-hair); background: var(--color-surface);">
                <nav class="frame py-[var(--space-md)] flex flex-col gap-[var(--space-xs)] font-[var(--font-mono)] text-[var(--text-sm)] uppercase tracking-widest">
                    {#each nav as { num, href, label }}
                        {@const active = isActive(href, $page.url.pathname)}
                        <a {href}
                           onclick={() => (mobileNavOpen = false)}
                           class="no-underline flex items-baseline gap-[var(--space-md)] py-[var(--space-md)]"
                           style="
                                text-decoration: none;
                                color: {active ? 'var(--color-ink)' : 'var(--color-mute)'};
                                border-bottom: 1px dashed var(--color-hair);
                                min-height: 44px;
                           "
                        >
                            <span style="color: var(--color-mute-2);">{num}</span>
                            <span>{label}</span>
                            {#if active}
                                <span class="ml-auto" style="color: var(--color-accent);">●</span>
                            {/if}
                        </a>
                    {/each}
                    <a href="https://github.com/tribixbite/beambam"
                       target="_blank" rel="noopener"
                       class="no-underline flex items-baseline gap-[var(--space-md)] py-[var(--space-md)]"
                       style="text-decoration: none; color: var(--color-mute); min-height: 44px;">
                        <span style="color: var(--color-mute-2);">↗</span>
                        <span>GitHub</span>
                    </a>
                </nav>
            </div>
        {/if}
    </header>

    <main class="flex-1">
        {@render children()}
    </main>

    <!-- Footer: small fixed metadata, like a drawing-title-block. -->
    <footer class="mt-[var(--space-6xl)]" style="border-top: 1px solid var(--color-hair);">
        <div class="frame py-[var(--space-lg)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest flex flex-wrap gap-x-[var(--space-2xl)] gap-y-[var(--space-sm)] items-baseline" style="color: var(--color-mute);">
            <span><span style="color: var(--color-mute-2);">PKG</span>&nbsp;beambam&nbsp;{VERSION}</span>
            <span class="hidden sm:inline"><span style="color: var(--color-mute-2);">REV</span>&nbsp;{REV}</span>
            <span><span style="color: var(--color-mute-2);">LIC</span>&nbsp;MIT</span>
            <span class="ml-auto flex gap-[var(--space-lg)]">
                <a href="https://github.com/tribixbite/beambam" style="text-decoration: none; color: inherit;">github</a>
                <a href="https://pypi.org/project/beambam/" style="text-decoration: none; color: inherit;">pypi</a>
                <a href="https://github.com/tribixbite/beambam/issues" style="text-decoration: none; color: inherit;">issues</a>
            </span>
        </div>
        <!-- Maker attribution, wrench pattern -->
        <div class="frame pb-[var(--space-lg)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest text-center" style="color: var(--color-mute-2);">
            <span aria-hidden="true" style="font-family: system-ui; letter-spacing: 0;">🪄💥</span>
            &nbsp;by&nbsp;<a href="https://tribixbite.com" target="_blank" rel="noopener" style="text-decoration: none; color: var(--color-mute);">tribixbite</a>
        </div>
    </footer>
</div>
