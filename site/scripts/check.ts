/**
 * `bun run check`: svelte-kit sync, then svelte-check, both run by this same bun.
 * Calling the tools' .bin shims instead fails on Termux (their `#!/usr/bin/env node`
 * shebang has no /usr/bin/env there), and so does an `a && b` script line through
 * Termux's bun wrapper; spawning the JS entry points directly works on Termux and CI alike.
 */
import { spawnSync } from 'node:child_process';

const steps: string[][] = [
    ['./node_modules/@sveltejs/kit/svelte-kit.js', 'sync'],
    ['./node_modules/svelte-check/bin/svelte-check', '--tsconfig', './tsconfig.json', ...process.argv.slice(2)]
];
for (const args of steps) {
    const r = spawnSync(process.execPath, args, { stdio: 'inherit', env: process.env });
    if (r.status !== 0) process.exit(r.status ?? 1);
}
