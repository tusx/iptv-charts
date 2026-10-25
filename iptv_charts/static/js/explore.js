/* Explore Movies / Explore Series.
 *
 * One file serves both pages. It reads the page kind from #explore-root and
 * renders TMDB discovery feeds from the local JSON API. All DOM is built with
 * textContent; titles, overviews and playlist names are provider data and
 * must never be treated as markup. The details dialog only reads from the
 * library: adding goes through the existing /api/movies and /api/series
 * endpoints, and playlist search is read-only. */

(() => {
  'use strict';

  const root = document.getElementById('explore-root');
  if (!root || typeof window.Manager === 'undefined') return;

  const { $, $$, get, post, element, imageUrl, open, setStatus } = window.Manager;

  const kind = root.dataset.kind === 'series' ? 'series' : 'movie';
  const playlistKind = root.dataset.playlistKind
    || (kind === 'movie' ? 'vod' : 'series');
  const endpoint = kind === 'movie' ? '/api/explore/movies' : '/api/explore/series';
  const configured = root.dataset.tmdbConfigured === '1';
  let feed = root.dataset.defaultFeed || 'trending';
  let windowKey = root.dataset.defaultWindow || 'week';
  let feedToken = 0;
  let playlistToken = 0;
  let currentItem = null;

  const grid = $('#explore-grid');
  const state = $('#explore-state');
  const windowGroup = $('#explore-window');
  const tabs = $$('[data-feed]', root);
  const windowButtons = $$('[data-window]', root);
  if (!grid || !state) return;

  // ------------------------------------------------------------- dialog refs
  const dialog = $('#explore-dialog');
  const dialogTitle = $('#explore-dialog-title');
  const detailPoster = $('#explore-detail-poster');
  const detailHeading = $('#explore-detail-heading');
  const detailFacts = $('#explore-detail-facts');
  const detailOverview = $('#explore-detail-overview');
  const libraryNote = $('#explore-library-note');
  const libraryLink = $('#explore-library-link');
  const addForm = $('#explore-add-form');
  const addStatus = $('#explore-add-status');
  const addButton = $('#explore-add-btn');
  const profileSelect = $('#explore-profile');
  const availabilitySelect = $('#explore-availability');
  const monitorModeSelect = $('#explore-monitor-mode');
  const newSeasonsSelect = $('#explore-monitor-new-seasons');
  const monitoredCheck = $('#explore-monitored');
  const searchNowCheck = $('#explore-search-now');
  const sourceSelect = $('#explore-source');
  const playlistQuery = $('#explore-playlist-query');
  const playlistButton = $('#explore-playlist-search');
  const playlistStatus = $('#explore-playlist-status');
  const playlistResults = $('#explore-playlist-results');

  // ---------------------------------------------------------------- helpers
  function fillPoster(container, url, title) {
    if (!container) return;
    container.replaceChildren();
    if (url) {
      const img = document.createElement('img');
      img.src = url;
      img.alt = '';
      img.loading = 'lazy';
      img.addEventListener('error', () => {
        container.replaceChildren();
        container.append(element('span', 'poster-fallback', initial(title)));
      });
      container.append(img);
    } else {
      container.append(element('span', 'poster-fallback', initial(title)));
    }
  }

  function initial(title) {
    return String(title || '?').slice(0, 1).toUpperCase();
  }

  function feedLabel() {
    const active = tabs.find((tab) => tab.dataset.feed === feed);
    return active ? active.textContent.trim() : feed;
  }

  function showState(title, message, retry) {
    const content = document.createDocumentFragment();
    content.append(element('h2', '', title));
    if (message) content.append(element('p', '', message));
    if (typeof retry === 'function') {
      const button = element('button', 'btn btn-sm', 'Try again');
      button.type = 'button';
      button.addEventListener('click', retry);
      content.append(button);
    }
    state.replaceChildren(content);
    state.hidden = false;
    grid.replaceChildren();
  }

  function clearState() {
    state.hidden = true;
    state.replaceChildren();
  }

  function syncWindow() {
    if (windowGroup) windowGroup.classList.toggle('is-hidden', feed !== 'trending');
    windowButtons.forEach((button) => {
      const active = button.dataset.window === windowKey;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  function syncTabs(activeTab) {
    tabs.forEach((tab) => {
      const active = tab === activeTab;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }

  // ------------------------------------------------------------------ feeds
  async function loadFeed() {
    const token = ++feedToken;
    grid.setAttribute('aria-busy', 'true');
    showState(`Loading ${feedLabel().toLowerCase()} ${kind === 'movie' ? 'movies' : 'series'}…`);
    if (!configured) {
      grid.setAttribute('aria-busy', 'false');
      showState(
        'TMDB is not configured',
        'Add a TMDB API Read Access Token in Settings to browse Explore feeds.',
      );
      return;
    }

    const params = new URLSearchParams({ feed });
    if (feed === 'trending') params.set('window', windowKey);

    try {
      const payload = await get(`${endpoint}?${params.toString()}`);
      if (token !== feedToken) return;
      if (!payload.items || !payload.items.length) {
        grid.setAttribute('aria-busy', 'false');
        showState('Nothing to show', 'TMDB returned no titles for this feed.');
        return;
      }
      clearState();
      grid.setAttribute('aria-busy', 'false');
      payload.items.forEach((item) => grid.append(renderCard(item)));
    } catch (error) {
      if (token !== feedToken) return;
      grid.setAttribute('aria-busy', 'false');
      showState('Could not load this feed', error.message, loadFeed);
    }
  }

  function renderCard(item) {
    const card = element('button', 'explore-card');
    card.type = 'button';
    card.setAttribute('aria-label', `Details for ${item.title || 'this title'}`);

    const poster = element('span', 'poster-frame explore-card-poster');
    fillPoster(poster, item.poster || imageUrl('w342', item.poster_path), item.title);

    const body = element('span', 'explore-card-body');
    body.append(element('span', 'explore-card-title', item.title || 'Untitled'));
    const footer = [];
    if (item.year) footer.push(String(item.year));
    if (item.rating !== null && item.rating !== undefined) {
      footer.push(`★ ${Number(item.rating).toFixed(1)}`);
    }
    body.append(element('span', 'explore-card-sub', footer.join(' · ') || 'Year unknown'));

    const badges = element('span', 'explore-card-badges');
    badges.append(element(
      'span',
      `badge ${item.library_url ? 'badge-downloaded' : 'badge-muted'}`,
      item.library_url ? 'In library' : (kind === 'movie' ? 'Movie' : 'Series'),
    ));
    body.append(badges);

    card.append(poster, body);
    card.addEventListener('click', () => openDetails(item));
    return card;
  }

  // ---------------------------------------------------------------- details
  function buildFacts(item) {
    const facts = [];
    if (item.year) facts.push(String(item.year));
    if (item.release_date) facts.push(String(item.release_date));
    if (item.rating !== null && item.rating !== undefined) {
      facts.push(`★ ${Number(item.rating).toFixed(1)}`);
    }
    if (item.original_language) facts.push(String(item.original_language).toUpperCase());
    facts.push(kind === 'movie' ? 'Movie' : 'Series');
    if (item.tmdb_id) facts.push(`TMDB #${item.tmdb_id}`);
    return facts.join(' · ');
  }

  function setLibraryState(item) {
    const inLibrary = Boolean(item && item.library_url);
    if (libraryNote) libraryNote.hidden = !inLibrary;
    if (inLibrary && libraryLink) libraryLink.href = item.library_url;
    if (addForm) addForm.hidden = inLibrary;
    if (addButton) {
      addButton.disabled = inLibrary || !profileSelect || !profileSelect.value;
      addButton.textContent = kind === 'movie' ? 'Add movie' : 'Add series';
    }
    setStatus(addStatus, '');
  }

  function openDetails(item) {
    currentItem = item;
    playlistToken += 1;
    if (dialogTitle) dialogTitle.textContent = item.title || 'Details';
    if (detailHeading) {
      const heading = item.original_title && item.original_title !== item.title
        ? item.original_title
        : (item.title || '');
      detailHeading.textContent = heading;
      detailHeading.hidden = !heading;
    }
    if (detailFacts) detailFacts.textContent = buildFacts(item);
    if (detailOverview) {
      detailOverview.textContent = item.overview || 'No overview is available yet.';
    }
    fillPoster(
      detailPoster,
      item.poster || imageUrl('w342', item.poster_path),
      item.title,
    );
    setLibraryState(item);
    if (playlistQuery) playlistQuery.value = item.title || '';
    if (playlistResults) playlistResults.replaceChildren();
    setStatus(playlistStatus, '');
    open(dialog);
  }

  // -------------------------------------------------------------- playlist
  function renderPlaylistResult(result) {
    const row = element('div', 'explore-playlist-result');
    row.append(element('strong', '', result.name));

    const facts = [];
    if (result.year) facts.push(String(result.year));
    if (result.category) facts.push(String(result.category));
    if (result.rating !== null && result.rating !== undefined) {
      facts.push(`★ ${Number(result.rating).toFixed(1)}`);
    }
    if (facts.length) row.append(element('span', 'explore-card-sub', facts.join(' · ')));
    if (result.plot) row.append(element('p', 'explore-playlist-plot', result.plot));

    const links = element('div', 'explore-playlist-links');
    const browse = element('a', 'btn btn-sm btn-ghost', 'Open playlist');
    browse.href = result.browse_url;
    links.append(browse);
    if (result.library_url) {
      const library = element('a', 'btn btn-sm', 'In library');
      library.href = result.library_url;
      links.append(library);
    }
    row.append(links);
    return row;
  }

  async function searchPlaylist() {
    if (!currentItem) return;
    const sourceId = sourceSelect ? sourceSelect.value : '';
    const query = playlistQuery ? playlistQuery.value.trim() : '';
    if (!sourceId) {
      setStatus(playlistStatus, 'Choose a playlist first.', 'bad');
      return;
    }
    if (!query) {
      setStatus(playlistStatus, 'Type a title to search.', 'bad');
      return;
    }

    const token = ++playlistToken;
    playlistResults.replaceChildren();
    setStatus(playlistStatus, 'Searching this playlist…');
    if (playlistButton) playlistButton.disabled = true;

    try {
      const params = new URLSearchParams({
        source_id: sourceId,
        kind: playlistKind,
        q: query,
      });
      const payload = await get(`/api/explore/playlist-search?${params.toString()}`);
      if (token !== playlistToken) return;
      if (!payload.items || !payload.items.length) {
        setStatus(playlistStatus, 'No matches in this playlist.', 'bad');
        return;
      }
      const noun = payload.total === 1 ? 'match' : 'matches';
      setStatus(playlistStatus, `${payload.total} ${noun} in the playlist catalog.`);
      payload.items.forEach((result) => playlistResults.append(renderPlaylistResult(result)));
    } catch (error) {
      if (token !== playlistToken) return;
      setStatus(playlistStatus, error.message, 'bad');
    } finally {
      if (token === playlistToken && playlistButton) playlistButton.disabled = false;
    }
  }

  // ------------------------------------------------------------------- add
  async function addToCollection() {
    if (!currentItem) return;
    const item = currentItem;
    if (!profileSelect || !profileSelect.value) {
      setStatus(addStatus, 'Create a quality profile first.', 'bad');
      return;
    }

    const payload = {
      tmdb_id: item.tmdb_id,
      quality_profile_id: Number(profileSelect.value),
      monitored: monitoredCheck ? monitoredCheck.checked : true,
      search: searchNowCheck ? searchNowCheck.checked : true,
    };
    if (kind === 'movie') {
      payload.minimum_availability = availabilitySelect
        ? availabilitySelect.value
        : 'released';
    } else {
      payload.monitor_mode = monitorModeSelect ? monitorModeSelect.value : 'all';
      payload.monitor_new_seasons = newSeasonsSelect
        ? newSeasonsSelect.value
        : 'all';
    }

    const original = addButton ? addButton.textContent : '';
    if (addButton) {
      addButton.disabled = true;
      addButton.textContent = 'Adding…';
    }
    setStatus(addStatus, 'Adding to the collection…');

    try {
      const url = kind === 'movie' ? '/api/movies' : '/api/series';
      const data = await post(url, payload);
      const entity = (kind === 'movie' ? data.movie : data.series) || {};
      if (entity.id) {
        const base = root.dataset.detailBase
          || (kind === 'movie' ? '/movies/' : '/series/');
        item.library_id = entity.id;
        item.library_url = `${base}${entity.id}`;
        if (libraryLink) libraryLink.href = item.library_url;
        if (libraryNote) libraryNote.hidden = false;
      }
      if (addForm) addForm.hidden = true;
      setStatus(
        addStatus,
        entity.title ? `Added “${entity.title}” to your library.` : 'Added to your library.',
        'ok',
      );
    } catch (error) {
      setStatus(addStatus, error.message, 'bad');
      if (addButton) addButton.disabled = false;
    } finally {
      if (addButton) addButton.textContent = original;
    }
  }

  // ----------------------------------------------------------------- wiring
  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      if (tab.dataset.feed === feed) return;
      feed = tab.dataset.feed;
      syncTabs(tab);
      syncWindow();
      loadFeed();
    });
  });

  windowButtons.forEach((button) => {
    button.addEventListener('click', () => {
      if (button.dataset.window === windowKey) return;
      windowKey = button.dataset.window;
      syncWindow();
      if (feed === 'trending') loadFeed();
    });
  });

  playlistButton?.addEventListener('click', searchPlaylist);
  playlistQuery?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      searchPlaylist();
    }
  });
  addButton?.addEventListener('click', addToCollection);

  dialog?.addEventListener('close', () => {
    playlistToken += 1;
    currentItem = null;
  });

  syncTabs(tabs.find((tab) => tab.dataset.feed === feed));
  syncWindow();
  loadFeed();
})();
