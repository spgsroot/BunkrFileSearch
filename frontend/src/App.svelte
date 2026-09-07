<script>
  import { onMount } from 'svelte';
  import { getStats, fmtTs } from './lib/api.js';
  import Browse from './Browse.svelte';
  import Search from './Search.svelte';
  import Support from './Support.svelte';

  let view = $state('browse');
  let statsData = $state(null);

  onMount(async () => {
    try {
      statsData = await getStats();
    } catch {
      statsData = { error: true };
    }
  });
</script>

<div class="layout">
  <aside class="rail">
    <div class="brand"><a href="/">Bunkr File Search</a></div>
    <a href="https://t.me/llmandagi" target="_blank" rel="noopener">Telegram</a>
    <a href="https://github.com/spgsroot/BunkrFileSearch" target="_blank" rel="noopener">GitHub Repo</a>
    <div id="statsline">
      {#if statsData && !statsData.error}
        <b>{statsData.albums_indexed.toLocaleString()}</b> albums ·
        <b>{statsData.files.toLocaleString()}</b> files ·
        {(statsData.db_bytes / 1048576).toFixed(1)} MB<br />
        indexed: {fmtTs(statsData.last_indexed_at)}
      {:else if statsData && statsData.error}
        server unavailable
      {/if}
    </div>
  </aside>

  <main class="wrap">
    <header class="topbar">
      <h1><a href="/">{view === 'browse' ? 'Latest albums' : 'Search'}</a></h1>
      <nav class="viewnav" aria-label="View">
        <button class:active={view === 'browse'} onclick={() => (view = 'browse')}>Browse</button>
        <button class:active={view === 'search'} onclick={() => (view = 'search')}>Search</button>
      </nav>
    </header>

    {#if view === 'browse'}
      <Browse />
    {:else}
      <Search />
    {/if}
  </main>
</div>

<Support />
