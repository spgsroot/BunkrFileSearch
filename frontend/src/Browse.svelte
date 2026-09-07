<script>
  import { onMount } from 'svelte';
  import { listAlbums, randomAlbum, albumUrl } from './lib/api.js';
  import Thumb from './Thumb.svelte';

  let items = $state([]);
  let page = $state(1);
  let done = $state(false);
  let loading = $state(false);
  let total = $state(0);
  let view = $state('grid');
  let error = $state('');

  async function load(reset = false) {
    if (loading) return;
    if (reset) {
      items = [];
      page = 1;
      done = false;
      total = 0;
      error = '';
    }
    loading = true;
    try {
      const d = await listAlbums(page, 30);
      const results = d.results || [];
      const rows = results.filter(
        (r) => r.indexed && !r.dead && Number(r.real_files || 0) > 0
      );
      items = [...items, ...rows];
      if (d.total) total = Number(d.total);
      if (results.length) page++;
      done = results.length < 30;
    } catch (e) {
      error = e.message;
      done = true;
    } finally {
      loading = false;
    }
  }

  function openRandom() {
    randomAlbum()
      .then((a) => window.open(albumUrl(a.bunkr_id), '_blank', 'noopener'))
      .catch(() => {});
  }

  onMount(() => load(true));
</script>

<div id="browsetools">
  <h2>
    Latest albums
    {#if total}<small>· {total.toLocaleString()} total</small>{/if}
  </h2>
  <div class="side">
    <button class="toolbtn accent" type="button" onclick={openRandom}>Random</button>
    <div class="viewtoggle" role="group" aria-label="View">
      <button class:active={view === 'grid'} onclick={() => (view = 'grid')}>Grid</button>
      <button class:active={view === 'list'} onclick={() => (view = 'list')}>List</button>
    </div>
  </div>
</div>

<div id="albums" class:grid={view === 'grid'} class:list={view === 'list'}>
  {#if view === 'grid'}
    {#each items as a (a.bunkr_id)}
      <a class="acard" href={albumUrl(a.bunkr_id)} target="_blank" rel="noopener">
        <Thumb thumb={a.thumb} title={a.title || a.bunkr_id} />
        <span class="atitle">{a.title || a.bunkr_id}</span>
        <span class="ameta">{Number(a.real_files || a.file_count || 0).toLocaleString()} files</span>
      </a>
    {/each}
  {:else}
    {#each items as a (a.bunkr_id)}
      <a class="arow" href={albumUrl(a.bunkr_id)} target="_blank" rel="noopener">
        <Thumb thumb={a.thumb} title={a.title || a.bunkr_id} />
        <span class="aname">{a.title || a.bunkr_id}</span>
        <span class="ameta">{Number(a.real_files || a.file_count || 0).toLocaleString()} files</span>
      </a>
    {/each}
  {/if}

  {#if !items.length && done}
    <div class="bmsg">{error ? `Unable to load albums: ${error}` : 'No indexed albums yet.'}</div>
  {/if}
</div>

{#if !done && items.length}
  <div id="loadmorewrap">
    <button id="loadmore" class="toolbtn" disabled={loading} onclick={() => load(false)}>
      Load more
    </button>
  </div>
{/if}
