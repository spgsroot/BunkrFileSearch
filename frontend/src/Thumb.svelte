<script>
  let { thumb = '', title = '' } = $props();
  let letter = $derived(((title || '?').trim().charAt(0) || '?').toUpperCase());
</script>

<span class="thumb">
  <span class="letter">{letter}</span>
  {#if thumb}
    <img loading="lazy" src={thumb} alt="" onerror={(e) => e.currentTarget.remove()} />
  {/if}
</span>
