<script lang="ts">
    /**
     * Interactive 3D preview of a loaded model on its build plate.
     *
     * Touch: one finger drags sideways to rotate (an up / down swipe scrolls the page: the preview is
     * up to 60vh, and a phone page must not get stuck on it), two fingers pinch to zoom and drag to
     * pan; mouse: left drag
     * rotates, wheel zooms, right drag pans. Kept light for a phone: one BufferGeometry per
     * part with byte vertex colours and precomputed normals, Lambert shading, no shadows, and
     * frames are drawn only while the view moves. three.js is loaded on first use so the
     * rest of the site never downloads it.
     */
    import { onDestroy, onMount } from 'svelte';
    import type * as THREE_NS from 'three';
    import type { OrbitControls as OrbitControlsT } from 'three/examples/jsm/controls/OrbitControls.js';
    import { bounds, vertexColours, type LoadedModel, type PreviewPart } from './model';

    interface Props {
        model: LoadedModel | null;
        /** Colour of each filament (index 0 = filament 1). */
        palette: string[];
        /** When true a tap (not a drag) picks a triangle instead of only orbiting. */
        paintMode?: boolean;
        /** A tap in paint mode: the part, the triangle index in its mesh, and the hit point. */
        onpick?: (part: PreviewPart, face: number, point: [number, number, number]) => void;
    }
    let { model, palette, paintMode = false, onpick }: Props = $props();

    let wrap: HTMLDivElement | undefined = $state();
    let canvas: HTMLCanvasElement | undefined = $state();
    let failed = $state('');
    let ready = $state(false);

    let THREE: typeof THREE_NS | null = null;
    let renderer: THREE_NS.WebGLRenderer | null = null;
    let scene: THREE_NS.Scene | null = null;
    let camera: THREE_NS.PerspectiveCamera | null = null;
    let controls: OrbitControlsT | null = null;
    let material: THREE_NS.MeshLambertMaterial | null = null;
    let modelGroup: THREE_NS.Group | null = null;
    let bedGroup: THREE_NS.Group | null = null;
    let observer: ResizeObserver | null = null;
    let raf = 0;
    // Height ("layer") clip: show the model up to a Z height, scrubbed with the slider.
    let clipPlane: THREE_NS.Plane | null = null;
    let modelZ = $state<[number, number]>([0, 1]); // model Z bounds (min, max)
    let clipFrac = $state(1); // 1 = show the whole height
    let shown: LoadedModel | null = null;
    let reduceMotion = false;

    /** Draw a frame; keep drawing while damping is still moving the camera. */
    function requestRender(): void {
        if (raf || !renderer) return;
        raf = requestAnimationFrame(() => {
            raf = 0;
            if (!renderer || !scene || !camera || !controls) return;
            const moving = controls.update();
            renderer.render(scene, camera);
            if (moving && controls.enableDamping) requestRender();
        });
    }

    function resize(): void {
        if (!renderer || !camera || !wrap) return;
        const w = Math.max(1, wrap.clientWidth);
        const h = Math.max(1, wrap.clientHeight);
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        requestRender();
    }

    /** A bed outline and 10 mm grid (50 mm lines brighter), in the plate's own bounds. */
    function buildBed(bed: LoadedModel['bed']): THREE_NS.Group {
        const T = THREE!;
        const g = new T.Group();
        const w = bed.maxX - bed.minX;
        const d = bed.maxY - bed.minY;
        const plate = new T.Mesh(
            new T.PlaneGeometry(w, d),
            new T.MeshBasicMaterial({ color: 0x1c1a17 })
        );
        plate.position.set(bed.minX + w / 2, bed.minY + d / 2, -0.05);
        g.add(plate);
        const minor: number[] = [];
        const major: number[] = [];
        for (let x = Math.ceil(bed.minX / 10) * 10; x <= bed.maxX + 1e-6; x += 10) {
            (Math.round(x) % 50 === 0 ? major : minor).push(x, bed.minY, 0, x, bed.maxY, 0);
        }
        for (let y = Math.ceil(bed.minY / 10) * 10; y <= bed.maxY + 1e-6; y += 10) {
            (Math.round(y) % 50 === 0 ? major : minor).push(bed.minX, y, 0, bed.maxX, y, 0);
        }
        const lines = (pts: number[], colour: number): THREE_NS.LineSegments => {
            const geo = new T.BufferGeometry();
            geo.setAttribute('position', new T.Float32BufferAttribute(pts, 3));
            return new T.LineSegments(geo, new T.LineBasicMaterial({ color: colour }));
        };
        g.add(lines(minor, 0x2a2723), lines(major, 0x45403a));
        const edge = [bed.minX, bed.minY, 0, bed.maxX, bed.minY, 0, bed.maxX, bed.minY, 0, bed.maxX, bed.maxY, 0,
            bed.maxX, bed.maxY, 0, bed.minX, bed.maxY, 0, bed.minX, bed.maxY, 0, bed.minX, bed.minY, 0];
        g.add(lines(edge, 0x8a6a3a));
        // A short amber tick marks the front edge (the side facing you on the printer).
        g.add(lines([bed.minX + w / 2 - 15, bed.minY - 3, 0, bed.minX + w / 2 + 15, bed.minY - 3, 0], 0xe8a33d));
        return g;
    }

    function disposeGroup(g: THREE_NS.Object3D | null): void {
        g?.traverse((o) => {
            const m = o as THREE_NS.Mesh;
            m.geometry?.dispose();
            const mat = m.material as THREE_NS.Material | THREE_NS.Material[] | undefined;
            if (mat && mat !== material) (Array.isArray(mat) ? mat : [mat]).forEach((x) => x.dispose());
        });
        g?.removeFromParent();
    }

    /** Put the camera in front of and above the model, framing it and the plate's centre. */
    export function resetView(): void {
        if (!THREE || !camera || !controls || !shown) return;
        const { min, max } = bounds(shown.parts);
        const b = shown.bed;
        const lo = Number.isFinite(min[0]) ? min : [b.minX, b.minY, 0];
        const hi = Number.isFinite(max[0]) ? max : [b.maxX, b.maxY, 10];
        const centre = new THREE.Vector3((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2);
        const radius = Math.max(20, 0.5 * Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]));
        const fov = (camera.fov * Math.PI) / 180;
        const fit = Math.min(fov, 2 * Math.atan(Math.tan(fov / 2) * camera.aspect));
        const dist = (radius / Math.sin(fit / 2)) * 1.15;
        const dir = new THREE.Vector3(0.35, -1, 0.75).normalize();
        camera.position.copy(centre).addScaledVector(dir, dist);
        camera.near = Math.max(0.5, dist / 200);
        camera.far = dist * 20 + 2000;
        camera.updateProjectionMatrix();
        controls.target.copy(centre);
        controls.update();
        requestRender();
    }

    function showModel(m: LoadedModel | null): void {
        if (!THREE || !scene) return;
        disposeGroup(modelGroup);
        disposeGroup(bedGroup);
        modelGroup = bedGroup = null;
        shown = m;
        if (!m) {
            requestRender();
            return;
        }
        bedGroup = buildBed(m.bed);
        scene.add(bedGroup);
        modelGroup = new THREE.Group();
        for (const p of m.parts) {
            const geo = new THREE.BufferGeometry();
            geo.setAttribute('position', new THREE.BufferAttribute(p.positions, 3));
            if (p.normals) geo.setAttribute('normal', new THREE.BufferAttribute(p.normals, 3));
            if (p.indices) geo.setIndex(new THREE.BufferAttribute(p.indices, 1));
            if (!p.normals) geo.computeVertexNormals();
            geo.setAttribute('color', new THREE.BufferAttribute(vertexColours(p, palette), 3, true));
            geo.computeBoundingSphere();
            const mesh = new THREE.Mesh(geo, material!);
            mesh.userData.part = p;
            modelGroup.add(mesh);
        }
        scene.add(modelGroup);
        const { min, max } = bounds(m.parts);
        modelZ = [Number.isFinite(min[2]) ? min[2] : 0, Number.isFinite(max[2]) ? max[2] : 1];
        clipFrac = 1; // a new model shows its whole height
        updateClip();
        resetView();
    }

    /** Set the clip plane to the slider height (shows the model up to it). */
    function updateClip(): void {
        if (!clipPlane) return;
        const [lo, hi] = modelZ;
        // A tiny margin above so the very top layer is not clipped at frac 1.
        clipPlane.constant = clipFrac >= 1 ? Infinity : lo + clipFrac * (hi - lo) + 1e-3;
        requestRender();
    }

    function recolour(colours: string[]): void {
        if (!modelGroup) return;
        for (const o of modelGroup.children) {
            const mesh = o as THREE_NS.Mesh;
            const attr = mesh.geometry.getAttribute('color') as THREE_NS.BufferAttribute;
            (attr.array as Uint8Array).set(vertexColours(mesh.userData.part, colours));
            attr.needsUpdate = true;
        }
        requestRender();
    }

    /** Redraw the parts' colours from their current paint states (after a paint edit). */
    export function repaint(): void {
        recolour(palette);
    }

    // --- tap-to-pick (paint mode) ------------------------------------------------
    let down: { x: number; y: number; t: number } | null = null;

    function onPointerDown(e: PointerEvent): void {
        // Only a primary press (left button / first finger) paints; a right-click or a pinch's second finger
        // must not. A non-primary press cancels any tap in progress so a pinch never lands as a paint.
        down = e.isPrimary && e.button === 0 ? { x: e.clientX, y: e.clientY, t: performance.now() } : null;
    }

    /** A tap (little movement, short) in paint mode picks the triangle under it. */
    function onPointerUp(e: PointerEvent): void {
        const d = down;
        down = null;
        if (!d || !e.isPrimary || !paintMode || !onpick || !THREE || !camera || !modelGroup || !canvas) return;
        if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > 8) return; // a drag / orbit, not a tap
        if (performance.now() - d.t > 700) return;
        const rect = canvas.getBoundingClientRect();
        const ndc = new THREE.Vector2(
            ((e.clientX - rect.left) / rect.width) * 2 - 1,
            -((e.clientY - rect.top) / rect.height) * 2 + 1
        );
        const ray = new THREE.Raycaster();
        ray.setFromCamera(ndc, camera);
        const hit = ray.intersectObjects(modelGroup.children, false)[0];
        if (!hit || hit.faceIndex == null) return;
        onpick((hit.object as THREE_NS.Mesh).userData.part as PreviewPart, hit.faceIndex, [
            hit.point.x,
            hit.point.y,
            hit.point.z
        ]);
    }

    onMount(() => {
        let cancelled = false;
        reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
        void (async () => {
            try {
                const T = await import('three');
                const { OrbitControls } = await import('three/examples/jsm/controls/OrbitControls.js');
                if (cancelled || !canvas) return;
                THREE = T;
                renderer = new T.WebGLRenderer({ canvas, antialias: window.devicePixelRatio < 2, powerPreference: 'default' });
                renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
                renderer.localClippingEnabled = true; // for the height ("layer") clip slider
                clipPlane = new T.Plane(new T.Vector3(0, 0, -1), Infinity); // keep z <= constant; ∞ = show all
                // Filament colours arrive as sRGB bytes: pass every colour through as given
                // (no linear conversion) so a swatch and the model show the same #RRGGBB.
                T.ColorManagement.enabled = false;
                renderer.outputColorSpace = T.LinearSRGBColorSpace;
                scene = new T.Scene();
                scene.background = new T.Color(0x110f0d);
                camera = new T.PerspectiveCamera(40, 1, 1, 5000);
                camera.up.set(0, 0, 1); // 3MF / printer coordinates are Z-up
                scene.add(camera);
                // three.js lights are physical (Lambert divides by π): ~1.2 + 1.8 puts a surface facing
                // the camera at about its true filament colour, with the hemisphere keeping undersides readable.
                scene.add(new T.HemisphereLight(0xffffff, 0x4a443c, 1.3));
                const key = new T.DirectionalLight(0xffffff, 1.8);
                key.position.set(0.4, 0.6, 1); // follows the camera: the side you look at is lit
                camera.add(key);
                material = new T.MeshLambertMaterial({ vertexColors: true, clippingPlanes: clipPlane ? [clipPlane] : [] });
                controls = new OrbitControls(camera, canvas);
                controls.enableDamping = !reduceMotion;
                controls.dampingFactor = 0.12;
                controls.screenSpacePanning = true;
                controls.touches = { ONE: T.TOUCH.ROTATE, TWO: T.TOUCH.DOLLY_PAN };
                controls.addEventListener('change', requestRender);
                // OrbitControls sets `touch-action: none`, which turns the whole canvas into a scroll trap on a
                // phone. pan-y gives vertical one-finger swipes back to the page; sideways drags and pinches
                // still reach the controls.
                canvas.style.touchAction = 'pan-y';
                // three.js rebuilds its GPU state after a lost context (common on phones under
                // memory pressure); with on-demand frames the view must be redrawn by hand.
                canvas.addEventListener('webglcontextrestored', () => requestRender());
                canvas.addEventListener('pointerdown', onPointerDown);
                canvas.addEventListener('pointerup', onPointerUp);
                observer = new ResizeObserver(resize);
                if (wrap) observer.observe(wrap);
                resize();
                ready = true;
                showModel(model);
            } catch (e) {
                failed = `3D preview unavailable on this device (${e instanceof Error ? e.message : String(e)}). You can still slice and print.`;
            }
        })();
        return () => {
            cancelled = true;
        };
    });

    // A new model replaces the scene; a new palette only rewrites the colour attribute.
    $effect(() => {
        const m = model;
        if (ready && m !== shown) showModel(m);
    });
    $effect(() => {
        const p = palette;
        if (ready && shown) recolour(p);
    });
    $effect(() => {
        clipFrac; // re-run when the height slider moves
        if (ready && shown) updateClip();
    });

    onDestroy(() => {
        if (raf) cancelAnimationFrame(raf);
        observer?.disconnect();
        controls?.dispose();
        disposeGroup(modelGroup);
        disposeGroup(bedGroup);
        material?.dispose();
        renderer?.dispose();
        renderer = null;
    });
</script>

<div class="pv" class:painting={paintMode} bind:this={wrap}>
    <canvas bind:this={canvas} aria-label="3D preview of the model on the build plate. Drag sideways to rotate, pinch to zoom."></canvas>
    {#if failed}
        <div class="pv-note pp-msg warn">{failed}</div>
    {:else if !model}
        <div class="pv-note pp-muted">Drop a file to see it here.</div>
    {/if}
    {#if model && !failed}
        <button type="button" class="pp-btn pv-reset" onclick={resetView}>Reset view</button>
        <div class="pv-layer" title="Show the model up to this height">
            <input
                type="range"
                min="0"
                max="1"
                step="0.01"
                bind:value={clipFrac}
                aria-label="Height shown"
                class="pv-layer-range"
            />
            <span class="pv-layer-val pp-mono">
                {clipFrac >= 1 ? 'all' : `${(modelZ[0] + clipFrac * (modelZ[1] - modelZ[0])).toFixed(1)} mm`}
            </span>
        </div>
    {/if}
</div>

<style>
    .pv {
        position: relative;
        width: 100%;
        height: clamp(280px, 62vw, 520px);
        max-height: 60vh;
        min-height: 260px;
        border: 1px solid var(--color-hair);
        background: #110f0d;
        overflow: hidden;
    }
    canvas {
        display: block;
        width: 100%;
        height: 100%;
        touch-action: pan-y;
    }
    .pv.painting canvas {
        cursor: crosshair;
    }
    .pv-note {
        position: absolute;
        inset: auto var(--space-md) var(--space-md) var(--space-md);
        pointer-events: none;
        margin: 0;
    }
    .pv-reset {
        position: absolute;
        top: var(--space-sm);
        right: var(--space-sm);
        background: color-mix(in oklab, var(--color-surface) 80%, transparent);
    }
    /* Vertical height ("layer") slider down the right edge. */
    .pv-layer {
        position: absolute;
        right: var(--space-sm);
        top: 3rem;
        bottom: 2.2rem;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--space-xs);
    }
    .pv-layer-range {
        writing-mode: vertical-lr;
        direction: rtl; /* top = full height, bottom = base */
        width: 24px;
        flex: 1;
        accent-color: var(--color-accent);
        cursor: pointer;
        touch-action: none; /* the slider owns vertical drags, not the page */
    }
    .pv-layer-val {
        font-size: 0.7rem;
        color: var(--color-mute);
        background: color-mix(in oklab, var(--color-surface) 80%, transparent);
        padding: 1px 3px;
        border-radius: 3px;
        white-space: nowrap;
    }
</style>
