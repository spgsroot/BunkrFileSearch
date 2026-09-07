<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    searchFiles,
    searchAlbumTitles,
    PER_PAGE,
    fmtSize,
    fmtDate,
    albumUrl,
  } from './lib/api.js';
  import Thumb from './Thumb.svelte';

  let query = $state('');
  let q = $state('');
  let media = $state('');
  let extRaw = $state('');
  let ext = $state('');
  let sort = $state('relevance');

  let page = $state(1);
  let cursors = $state([null]);
  let results = $state([]);
  let groups = $state([]);
  let albumMatches = $state([]);
  let total = $state(0);
  let truncated = $state(false);
  let hasMore = $state(false);
  let nextCursor = $state(null);
  let loading = $state(false);
  let error = $state('');
  let view = $state('grid');

  let generation = 0;
  let debounceTimer = null;
  let extTimer = null;
  let inputEl;

  function buildGroups(items) {
    const map = new Map();
    for (const f of items) {
      let g = map.get(f.album_id);
      if (!g) {
        g = {
          id: f.album_id,
          title: f.album_title || f.album_id,
          thumb: f.album_thumb || '',
          files: [],
          open: false,
        };
        map.set(f.album_id, g);
      }
      g.files.push(f);
    }
    return [...map.values()];
  }

  async function runSearch() {
    const gen = ++generation;
    if (!q.trim()) {
      results = [];
      groups = [];
      albumMatches = [];
      total = 0;
      truncated = false;
      hasMore = false;
      nextCursor = null;
      error = '';
      loading = false;
      return;
    }
    loading = true;
    error = '';
    const cursor = cursors[page - 1];
    const params = new URLSearchParams({ media, sort, page: String(page), per: String(PER_PAGE) });
    params.set('q', q.trim());
    if (ext) params.set('ext', ext);
    if (cursor) params.set('cursor', cursor);

    try {
      const [d, albums] = await Promise.all([
        searchFiles(params),
        searchAlbumTitles(q.trim(), 5),
      ]);
      if (gen !== generation) return;
      results = d.results || [];
      groups = buildGroups(results);
      total = Number(d.total) || 0;
      truncated = Boolean(d.truncated);
      hasMore = Boolean(d.has_more);
      nextCursor = d.next_cursor || null;
      albumMatches = albums && albums.results ? albums.results.slice(0, 5) : [];
    } catch (e) {
      if (gen === generation) {
        error = e.message;
        results = [];
        groups = [];
        total = 0;
        albumMatches = [];
      }
    } finally {
      if (gen === generation) loading = false;
    }
  }

  function resetAndSearch() {
    page = 1;
    cursors = [null];
    runSearch();
  }

  function onQueryInput(e) {
    query = e.currentTarget.value;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      q = query;
      resetAndSearch();
    }, 400);
  }

  function onExtInput(e) {
    extRaw = e.currentTarget.value;
    clearTimeout(extTimer);
    extTimer = setTimeout(() => {
      ext = extRaw.trim().replace(/^\./, '').toLowerCase();
      resetAndSearch();
    }, 400);
  }

  function onFilterChange() {
    resetAndSearch();
  }

  function nextPage() {
    if (loading || !hasMore || !nextCursor) return;
    cursors[page] = nextCursor;
    page++;
    runSearch();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function prevPage() {
    if (loading || page <= 1) return;
    page--;
    runSearch();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function toggleGroup(g) {
    g.open = !g.open;
  }

  onMount(() => inputEl && inputEl.focus());
  onDestroy(() => {
    clearTimeout(debounceTimer);
    clearTimeout(extTimer);
  });
</script>

<div class="searchline" id="searchline" class:loading>
  <input
    id="q"
    type="search"
    placeholder="Search filenames…"
    autocomplete="off"
    spellcheck="false"
    aria-label="Search filenames"
    value={query}
    oninput={onQueryInput}
    bind:this={inputEl}
  />
  <select title="Filter by media type" aria-label="Media type" bind:value={media} onchange={onFilterChange}>
    <option value="">all media</option>
    <option value="image">images</option>
    <option value="video">videos</option>
    <option value="audio">audio</option>
    <option value="other">other</option>
  </select>
  <input
    id="ext"
    type="search"
    inputmode="text"
    maxlength="16"
    placeholder="ext"
    autocomplete="off"
    spellcheck="false"
    aria-label="Filter by file extension"
    value={extRaw}
    oninput={onExtInput}
  />
  <select title="Sort results" aria-label="Sort results" bind:value={sort} onchange={onFilterChange}>
    <option value="relevance">relevance</option>
    <option value="newest">newest</option>
    <option value="oldest">oldest</option>
    <option value="size">largest</option>
  </select>
</div>
<p class="searchhint">
  3+ characters search anywhere, shorter queries match prefixes. Use quotes for an
  exact phrase, or filter by extension.
</p>

{#if albumMatches.length}
  <div id="albumsec">
    <h2>Album matches</h2>
    {#each albumMatches as a (a.bunkr_id)}
      <div class="row album">
        <div class="name">{a.title || a.bunkr_id}</div>
        <div class="meta">
          <a href={albumUrl(a.bunkr_id)} target="_blank" rel="noopener">View album</a>
          · {Number(a.real_files || a.file_count || 0).toLocaleString()} files
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if error}
  <div id="empty">Unable to load results: {error}</div>
{:else if loading && !groups.length}
  <div id="empty">Searching…</div>
{:else if q && !groups.length}
  <div id="empty">No matching files found.</div>
{:else if groups.length}
  <div id="resulttools">
    <h2>
      File matches
      <small>· {total.toLocaleString()}{truncated ? '+' : ''} total</small>
    </h2>
    <div class="viewtoggle" role="group" aria-label="Results view">
      <button class:active={view === 'grid'} onclick={() => (view = 'grid')}>Grid</button>
      <button class:active={view === 'list'} onclick={() => (view = 'list')}>List</button>
    </div>
  </div>

  <div id="results" class:grid={view === 'grid'} class:list={view === 'list'}>
    {#if view === 'grid'}
      {#each groups as g (g.id)}
        <div class="groupcard">
          <Thumb thumb={g.thumb} title={g.title} />
          <div class="gbody">
            <div class="gtop">
              <a class="gtitle" href={albumUrl(g.id)} target="_blank" rel="noopener">{g.title}</a>
              <span class="gcount">{g.files.length} match{g.files.length === 1 ? '' : 'es'}</span>
            </div>
            <div class="gfiles">
              {#each g.files as f (f.id)}
                <div class="row">
                  <div class="name">{f.search_name || f.storage || f.slug}</div>
                  <div class="meta">
                    {f.media || 'file'}{f.size ? ` · ${fmtSize(f.size)}` : ''} · {fmtDate(f.uploaded_at)}
                  </div>
                </div>
              {/each}
            </div>
            <a class="openlink" href={albumUrl(g.id)} target="_blank" rel="noopener">View album</a>
          </div>
        </div>
      {/each}
    {:else}
      {#each groups as g (g.id)}
        <div class="group" class:closed={!g.open}>
          <div
            class="ghead"
            role="button"
            tabindex="0"
            aria-expanded={g.open}
            onclick={(e) => {
              if (e.target.closest('a')) return;
              toggleGroup(g);
            }}
            onkeydown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                toggleGroup(g);
              }
            }}
          >
            <span class="chev" aria-hidden="true">▾</span>
            <Thumb thumb={g.thumb} title={g.title} />
            <span class="gtitle">{g.title}</span>
            <span class="gcount">{g.files.length} match{g.files.length === 1 ? '' : 'es'}</span>
            <a class="openlink" href={albumUrl(g.id)} target="_blank" rel="noopener">View album</a>
          </div>
          <div class="gfiles">
            {#each g.files as f (f.id)}
              <div class="row">
                <div class="name">{f.search_name || f.storage || f.slug}</div>
                <div class="meta">
                  {f.media || 'file'}{f.size ? ` · ${fmtSize(f.size)}` : ''} · {fmtDate(f.uploaded_at)}
                </div>
              </div>
            {/each}
          </div>
        </div>
      {/each}
    {/if}
  </div>

  <div class="pager" id="pager">
    <button type="button" disabled={loading || page <= 1} onclick={prevPage}>&larr; Previous</button>
    <button type="button" disabled={loading || !hasMore} onclick={nextPage}>Next &rarr;</button>
    <span>
      {total.toLocaleString()}{truncated ? '+' : ''} file{total === 1 ? '' : 's'} · page {page}
    </span>
  </div>
{/if}
