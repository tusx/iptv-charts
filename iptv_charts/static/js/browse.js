/* Browse page: search, filters, and the per-item action buttons.
 *
 * Results are fetched a page at a time and rendered as DOM built in JS. Text
 * always goes in via textContent -- provider-supplied names, plots and logos
 * are not to be trusted as markup. */

(() => {
  'use strict';

  const PAGE_SIZE = 60;
  const SEARCH_DEBOUNCE = 220;

  const $ = (sel) => document.querySelector(sel);

  const results = $('#results');
  const summary = $('#result-summary');
  const progressNote = $('#progress-note');
  const moreBtn = $('#more-btn');
  const searchInput = $('#search');
  const searchClear = $('#search-clear');
  const categorySelect = $('#category');
  const sortSelect = $('#sort');

  const sourceId = Number(results.closest('[data-source-id]')?.dataset.sourceId
    || document.body.dataset.sourceId || window.SOURCE_ID);

  const state = { q: '', kind: '', category: '', sort: 'name', offset: 0, total: 0 };
  let searchTimer = null;
  let requestToken = 0;

  // ------------------------------------------------------------- categories
  async function loadCategories() {
    if (!categorySelect) return;
    try {
      // Scoped to the active kind: categories belong to one kind, so showing
      // VOD categories under the Live tab would offer filters matching
      // nothing.
      const params = new URLSearchParams();
      if (state.kind) params.set('kind', state.kind);
      const response = await fetch(`/api/sources/${sourceId}/categories?${params}`);
      const payload = await response.json();
      if (!payload.ok) return;

      categorySelect.innerHTML = '';
      const all = document.createElement('option');
      all.value = '';
      all.textContent = 'All categories';
      categorySelect.appendChild(all);

      for (const category of payload.categories) {
        const option = document.createElement('option');
        option.value = category.id;
        option.textContent = `${category.name} (${category.item_count.toLocaleString()})`;
        categorySelect.appendChild(option);
      }

      // Say so when the list is capped, rather than silently hiding
      // categories and leaving the user wondering where theirs went.
      if (payload.total > payload.shown) {
        const more = document.createElement('option');
        more.disabled = true;
        more.textContent = `… ${(payload.total - payload.shown).toLocaleString()} smaller categories — use search`;
        categorySelect.appendChild(more);
      }
    } catch { /* the dropdown is a convenience; search still works without it */ }
  }

  // ---------------------------------------------------------------- fetching
  function buildQuery() {
    const params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.kind) params.set('kind', state.kind);
    if (state.category) params.set('category', state.category);
    params.set('sort', state.sort);
    params.set('limit', PAGE_SIZE);
    params.set('offset', state.offset);
    return params.toString();
  }

  async function load({ append = false } = {}) {
    const token = ++requestToken;
    moreBtn.disabled = true;

    let payload;
    try {
      const response = await fetch(`/api/sources/${sourceId}/items?${buildQuery()}`);
      payload = await response.json();
    } catch {
      summary.textContent = 'Could not load items.';
      moreBtn.disabled = false;
      return;
    }

    // A slower earlier request must not overwrite a newer one's results.
    if (token !== requestToken) return;

    if (!payload.ok) {
      summary.textContent = payload.error || 'Could not load items.';
      moreBtn.disabled = false;
      return;
    }

    state.total = payload.total;
    if (!append) results.innerHTML = '';

    for (const item of payload.items) {
      results.appendChild(renderCard(item));
    }

    if (!payload.items.length && !append) {
      results.appendChild(renderEmpty());
    }

    const shown = append ? results.childElementCount : payload.items.length;
    summary.textContent = payload.total === 0
      ? 'No matches'
      : `${shown.toLocaleString()} of ${payload.total.toLocaleString()} items`;

    state.offset = payload.offset + payload.items.length;
    progressNote.textContent = payload.has_more
      ? `${(payload.total - state.offset).toLocaleString()} more`
      : '';
    moreBtn.hidden = !payload.has_more;
    moreBtn.disabled = false;
  }

  // ------------------------------------------------------------------ cards
  function renderEmpty() {
    const wrap = document.createElement('div');
    wrap.className = 'empty empty-inline';
    const heading = document.createElement('h2');
    heading.textContent = 'Nothing matches';
    const body = document.createElement('p');
    body.textContent = state.q
      ? `No items match “${state.q}”.`
      : 'This playlist has no items in this category.';
    wrap.append(heading, body);
    return wrap;
  }

  function renderCard(item) {
    const card = document.createElement('article');
    card.className = 'poster-card';

    // A button, not a link: this opens the player modal, so there is no page
    // to navigate to and middle-click/open-in-new-tab would be a lie.
    const art = document.createElement('button');
    art.type = 'button';
    art.className = 'poster-art';
    art.setAttribute('aria-label', `Play ${item.name}`);
    art.addEventListener('click', () => window.openPlayer(item));

    if (item.logo) {
      const img = document.createElement('img');
      img.loading = 'lazy';          // 60 images per page add up quickly
      img.decoding = 'async';
      img.alt = '';
      img.src = item.logo;
      img.addEventListener('error', () => img.remove());
      art.appendChild(img);
    }

    const playGlyph = document.createElement('span');
    playGlyph.className = 'poster-play';
    playGlyph.textContent = '▶';
    playGlyph.setAttribute('aria-hidden', 'true');
    art.appendChild(playGlyph);

    const kindBadge = document.createElement('span');
    kindBadge.className = `poster-kind poster-kind-${item.kind}`;
    kindBadge.textContent = item.kind === 'vod' ? 'VOD' : item.kind;
    art.appendChild(kindBadge);

    card.appendChild(art);

    const title = document.createElement('h3');
    title.className = 'poster-title';
    title.textContent = item.name;
    title.title = item.name;
    card.appendChild(title);

    const meta = document.createElement('p');
    meta.className = 'poster-meta';
    const bits = [];
    if (item.category_name) bits.push(item.category_name);
    if (item.rating) bits.push(`★ ${item.rating}`);
    if (item.duration) bits.push(item.duration);
    meta.textContent = bits.join(' · ') || '—';
    card.appendChild(meta);

    card.appendChild(renderActions(item));
    return card;
  }

  function renderActions(item) {
    const actions = document.createElement('div');
    actions.className = 'poster-actions';

    const play = document.createElement('button');
    play.className = 'btn btn-sm';
    play.textContent = '▶ Play';
    play.title = 'Play in the browser';
    play.addEventListener('click', () => window.openPlayer(item));

    const copy = document.createElement('button');
    copy.className = 'btn btn-sm';
    copy.textContent = '⧉ Copy';
    copy.title = item.stream_url
      ? 'Copy the stream link'
      : 'Copy link — resolved when you play';
    copy.addEventListener('click', () => copyLink(item, copy));

    actions.append(play, copy);

    // Only offered when a player was actually found. Showing a button that
    // does nothing is worse than not showing it.
    if (window.EXTERNAL_PLAYER_AVAILABLE) {
      // Short on a card; the tooltip names the program it will actually open.
      const external = document.createElement('button');
      external.className = 'btn btn-sm';
      external.textContent = '⤴ Player';
      external.title = `Play in ${window.EXTERNAL_PLAYER_LABEL || 'the external player'}`;
      external.addEventListener('click', () => playExternal(item, external));
      actions.appendChild(external);
    }

    return actions;
  }

  // ----------------------------------------------------------------- actions
  async function copyLink(item, button) {
    const original = button.textContent;
    let url = item.stream_url;

    if (!url) {
      // Series have no URL until an episode is chosen; ask the server.
      try {
        const response = await fetch(`/api/items/${item.id}/stream`);
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error);
        url = payload.url;
      } catch (error) {
        button.textContent = '✕';
        button.title = error.message;
        setTimeout(() => { button.textContent = original; }, 1800);
        return;
      }
    }

    const ok = await writeClipboard(url);
    button.textContent = ok ? '✓ Copied' : '✕ Failed';
    button.title = ok ? url : 'Clipboard unavailable — the link is in the console';
    if (!ok) console.info('Stream URL:', url);
    setTimeout(() => {
      button.textContent = original;
      button.title = '';
    }, 1500);
  }

  async function writeClipboard(text) {
    // navigator.clipboard needs a secure context. http://127.0.0.1 and
    // http://localhost both qualify, so this normally just works; the
    // textarea route covers anything unusual.
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch { /* fall through */ }

    try {
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.setAttribute('readonly', '');
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      const ok = document.execCommand('copy');
      scratch.remove();
      return ok;
    } catch {
      return false;
    }
  }

  async function playExternal(item, button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = '…';
    try {
      const response = await fetch(`/api/items/${item.id}/play`, { method: 'POST' });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error);
      button.textContent = '✓ Sent';
      button.title = `Playing in ${payload.player}`;
    } catch (error) {
      button.textContent = '✕';
      button.title = error.message;
    }
    setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, 1500);
  }

  // ------------------------------------------------------------------ events
  function reset() {
    state.offset = 0;
    load();
  }

  searchInput?.addEventListener('input', () => {
    searchClear.hidden = !searchInput.value;
    clearTimeout(searchTimer);
    // Debounced so a fast typist does not fire a query per keystroke.
    searchTimer = setTimeout(() => {
      state.q = searchInput.value.trim();
      reset();
    }, SEARCH_DEBOUNCE);
  });

  searchClear?.addEventListener('click', () => {
    searchInput.value = '';
    searchClear.hidden = true;
    state.q = '';
    reset();
    searchInput.focus();
  });

  document.querySelectorAll('#kind-tabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#kind-tabs .tab')
        .forEach((other) => other.classList.toggle('is-active', other === tab));
      state.kind = tab.dataset.kind;
      state.category = '';        // categories are per-kind; the old one is stale
      loadCategories();
      reset();
    });
  });

  categorySelect?.addEventListener('change', () => {
    state.category = categorySelect.value;
    reset();
  });

  sortSelect?.addEventListener('change', () => {
    state.sort = sortSelect.value;
    reset();
  });

  moreBtn?.addEventListener('click', () => load({ append: true }));

  // Infinite scroll, with the button kept as a fallback for anyone who
  // prefers it or whose viewport never reaches the sentinel.
  const sentinel = document.createElement('div');
  sentinel.className = 'scroll-sentinel';
  results.after(sentinel);

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting) && !moreBtn.hidden) {
      load({ append: true });
    }
  }, { rootMargin: '600px' });
  observer.observe(sentinel);

  $('#refresh-btn')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await fetch(`/api/sources/${sourceId}/sync`, { method: 'POST' });
      event.currentTarget.textContent = 'Syncing…';
      const poll = setInterval(async () => {
        const snapshot = await (await fetch('/api/sync/status')).json();
        if (!snapshot.active) {
          clearInterval(poll);
          window.location.reload();
        }
      }, 1000);
    } catch {
      event.currentTarget.disabled = false;
      event.currentTarget.textContent = 'Update';
    }
  });

  loadCategories();
  load();
})();
