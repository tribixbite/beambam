/**
 * Vite CLI wrapper that bypasses the #!/usr/bin/env node shebang.
 * On Termux, `node` is bionic-linked (reports process.platform === 'android'),
 * which breaks native binary resolution for rollup, lightningcss, etc.
 * Running via `bun` ensures process.platform === 'linux' and glibc-linked
 * native binaries load correctly.
 *
 * Usage: bun ./scripts/vite-cli.ts dev [--port 5174]
 *
 * Pattern copied from ~/git/torch — the same trick is needed for any
 * Vite-based site built on this device.
 */
import '../node_modules/vite/bin/vite.js';
