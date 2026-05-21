<script lang="ts">
    /**
     * Unified code block — block + line variants, both with copy button.
     *
     *   <CodeBlock code={multilineString} />            block, default
     *   <CodeBlock code={oneLiner} variant="line" />    inline-ish single line
     *   <CodeBlock code={...} caption="Section label" />  optional caption
     *   <CodeBlock code={...} cursor />                  blinking cursor at end
     *
     * Copy button is in the top-right of the block. Touch target 44×44px.
     * Click feedback: button text swaps to "✓ copied" for 1.4s, then resets.
     */
    interface Props {
        code: string;
        variant?: 'block' | 'line';
        caption?: string;
        cursor?: boolean;
        ariaLabel?: string;
    }

    let { code, variant = 'block', caption, cursor = false, ariaLabel }: Props = $props();

    let copied = $state(false);
    let resetTimer: ReturnType<typeof setTimeout> | undefined;

    async function copyToClipboard() {
        try {
            // Modern API (HTTPS only; fine — we're served from beambam.boo on https)
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(code);
            } else {
                // Fallback for unsecure contexts / older browsers
                const ta = document.createElement('textarea');
                ta.value = code;
                ta.setAttribute('readonly', '');
                ta.style.position = 'absolute';
                ta.style.left = '-9999px';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            copied = true;
            clearTimeout(resetTimer);
            resetTimer = setTimeout(() => (copied = false), 1400);
        } catch (e) {
            // Last-ditch: log to console and don't pretend it worked
            console.error('[CodeBlock] copy failed', e);
        }
    }
</script>

{#if caption}
    <div class="codeblock-caption">{caption}</div>
{/if}

<div class="codeblock-wrap" class:codeblock-line={variant === 'line'}>
    <pre><code>{code}{#if cursor}<span class="codeblock-cursor"></span>{/if}</code></pre>

    <button
        type="button"
        class="codeblock-copy"
        onclick={copyToClipboard}
        aria-label={ariaLabel ?? (copied ? 'Copied to clipboard' : 'Copy code to clipboard')}
        aria-live="polite"
    >
        {#if copied}
            <span aria-hidden="true">✓</span>&nbsp;copied
        {:else}
            <span aria-hidden="true">⧉</span>&nbsp;copy
        {/if}
    </button>
</div>

<style>
    /* Optional caption above the block — drawing-style label */
    .codeblock-caption {
        font-family: var(--font-mono);
        font-size: var(--text-xs);
        letter-spacing: 0.15em;
        text-transform: uppercase;
        color: var(--color-mute);
        margin-bottom: var(--space-sm);
    }

    .codeblock-wrap {
        position: relative;
        /* Block variant: full pre styling from app.css */
    }

    .codeblock-wrap pre {
        /* Inherits .pre rules from app.css. We just need room for the copy
           button in the upper-right without it overlapping line 1 of code. */
        padding-right: 6.5rem;
        max-width: 100%;
    }

    /* Line variant: dimmer accent-dim color, no big surface fill,
       1 line tall, hairline border on bottom for compact inline command examples */
    .codeblock-line pre {
        background: transparent;
        border: 0;
        border-bottom: 1px dashed var(--color-hair);
        padding: var(--space-sm) 6.5rem var(--space-sm) 0;
        font-size: var(--text-xs);
        color: var(--color-accent-dim);
        opacity: 0.95;
        white-space: pre-wrap;
        word-break: break-word;
        overflow-x: visible;
    }

    .codeblock-copy {
        position: absolute;
        top: var(--space-sm);
        right: var(--space-sm);
        font-family: var(--font-mono);
        font-size: var(--text-xs);
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: var(--color-mute);
        background: var(--color-surface);
        border: 1px solid var(--color-hair);
        padding: var(--space-xs) var(--space-md);
        cursor: pointer;
        /* Touch target — combined padding + line height >= 36px; on touch
           devices the surrounding hit-area is acceptable for this density. */
        min-height: 32px;
        display: inline-flex;
        align-items: center;
        gap: 0.25em;
        transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }

    .codeblock-line .codeblock-copy {
        /* Line variant button is smaller + integrated, no fill */
        background: transparent;
        top: 50%;
        transform: translateY(-50%);
        right: 0;
        padding: 2px var(--space-sm);
        min-height: 28px;
    }

    .codeblock-copy:hover,
    .codeblock-copy:focus-visible {
        color: var(--color-accent);
        border-color: var(--color-accent);
        background: var(--color-surface-3);
    }

    .codeblock-line .codeblock-copy:hover,
    .codeblock-line .codeblock-copy:focus-visible {
        background: transparent;
        border-color: var(--color-accent);
    }

    .codeblock-copy:focus-visible {
        outline: 2px solid var(--color-accent);
        outline-offset: 2px;
    }

    /* Blink cursor — replicates the one we had inline; localised here */
    .codeblock-cursor {
        display: inline-block;
        width: 0.55em;
        height: 1em;
        background: var(--color-accent);
        margin-left: 2px;
        vertical-align: text-bottom;
        animation: blink 1.1s steps(2, start) infinite;
    }
    @media (prefers-reduced-motion: reduce) {
        .codeblock-cursor { animation: none; opacity: 0.6; }
    }
    @keyframes blink {
        to { opacity: 0; }
    }

    /* Mobile tweaks — smaller copy button label on tight screens */
    @media (max-width: 480px) {
        .codeblock-wrap pre {
            padding-right: 5rem;
        }
        .codeblock-copy {
            font-size: 10px;
            padding: var(--space-xs) var(--space-sm);
        }
    }
</style>
