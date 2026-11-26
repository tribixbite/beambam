<script lang="ts">
    /**
     * A filament colour square. An empty colour shows a hatched "none" square.
     *   <Swatch colour="#EEBF31" text="1" />
     */
    import { textOn } from './colour';

    interface Props {
        colour: string;
        text?: string;
        size?: 'md' | 'lg';
        title?: string;
    }
    let { colour, text = '', size = 'md', title }: Props = $props();
</script>

<span
    class="pp-sw"
    class:lg={size === 'lg'}
    class:none={!colour}
    style:background={colour || undefined}
    style:color={colour ? textOn(colour) : undefined}
    title={title ?? (colour || 'no colour')}
    aria-hidden={title ? undefined : 'true'}
>{text}</span>
