/**
 * Focus for the page's inline confirms (Start print, Stop): they replace the button that opened them, so without
 * this the keyboard / screen-reader focus falls back to the page body and the confirm is never announced.
 */

export interface ConfirmFocus {
    /** The element to focus first (a selector inside the confirm); default: the confirm itself. Point it at the
     *  safe choice ("Keep printing"), never at a destructive one, so Enter cannot stop a print by accident. */
    initial?: string;
    /** Where focus goes when the confirm closes (a selector in the document): the button that opened it. */
    returnTo?: string;
    /** Escape closes the confirm. */
    onEscape?: () => void;
}

/** Svelte action: `<div role="dialog" use:confirmFocus={{ initial, returnTo, onEscape }}>`. */
export function confirmFocus(node: HTMLElement, opts: ConfirmFocus = {}): { update: (o: ConfirmFocus) => void; destroy: () => void } {
    let current = opts;
    if (!node.hasAttribute('tabindex')) node.tabIndex = -1;
    const first = (current.initial && node.querySelector<HTMLElement>(current.initial)) || node;
    first.focus();
    const onKey = (e: KeyboardEvent): void => {
        if (e.key !== 'Escape' || !current.onEscape) return;
        e.preventDefault();
        current.onEscape();
    };
    node.addEventListener('keydown', onKey);
    return {
        update: (o) => {
            current = o;
        },
        destroy: () => {
            node.removeEventListener('keydown', onKey);
            const back = current.returnTo;
            // Only when focus was inside the confirm (or got lost with it): never steal it from elsewhere.
            const hadFocus = node.contains(document.activeElement) || document.activeElement === document.body;
            if (back && hadFocus) setTimeout(() => document.querySelector<HTMLElement>(back)?.focus(), 0);
        }
    };
}
