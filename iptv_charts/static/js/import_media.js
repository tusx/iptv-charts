/* Import existing: scan the media roots, pick metadata, confirm imports.
 *
 * Plain DOM + fetch, like the rest of the app. Scanning is read-only; the
 * import button posts one reviewed file at a time so a conflict on one row
 * cannot hide the state of the others. */

(() => {
  'use strict';

  const { $, get, post, setStatus, element, formatBytes } = Manager;
  const page = $('#import-page');
  if (!page) return;

  const list = $('#import-list');
  const empty = $('#import-empty');
  const emptyTitle = $('#import-empty-title');
  const emptyText = $('#import-empty-text');
  const status = $('#import-status');
  const profileSelect = $('#import-profile');
  const scanButton = $('#import-scan');
  const readyButton = $('#import-ready');
  const seriesOptions = $('#import-series-options');
  const seriesMode = $('#import-series-mode');
  const seasonsBox = $('#import-seasons');
  const seasonList = $('#import-season-list');
  const rowTemplate = $('#import-row-template');

  const state = {
    tab: page.dataset.initialTab === 'series' ? 'series' : 'movie',
    items: { movie: [], series: [] },
    loaded: { movie: false, series: false },
    busy: false,
    profileId: Number(profileSelect?.value || page.dataset.defaultProfile || 0) || null,
    seriesMode: 'all',
    seasons: new Set(),
    tmdbConfigured: page.dataset.tmdbConfigured === '1',
  };

  const kindFor = (entry) => (entry.kind === 'episode' ? 'series' : 'movie');
  const currentItems = () => state.items[state.tab];

  function safeImage(url) {
    return typeof url === 'string' && url.startsWith('/api/images/tmdb/') ? url : null;
  }

  function parsedLabel(entry) {
    const parts = [];
    const title = entry.title || entry.name;
    parts.push(entry.year ? `${title} (${entry.year})` : title);
    if (entry.season !== null && entry.season !== undefined) {
      const code = `S${String(entry.season).padStart(2, '0')}`;
      const episode = entry.episode !== null && entry.episode !== undefined
        ? `E${String(entry.episode).padStart(2, '0')}` : '';
      parts.push(`${code}${episode}`);
    }
    if (entry.resolution && entry.resolution !== 'unknown') parts.push(entry.resolution);
    return parts.join(' · ');
  }

  function hasMatch(entry) {
    if (entry._match) return true;
    return kindFor(entry) === 'movie'
      ? Boolean(entry.matched_movie_id) : Boolean(entry.matched_series_id);
  }

  function canImport(entry) {
    if (!entry.importable || entry._imported || !hasMatch(entry)) return false;
    if (entry.kind === 'episode' && state.seriesMode === 'manual'
        && state.seasons.size === 0) {
      return false;
    }
    return true;
  }

  function updateReadyButton() {
    if (!readyButton) return;
    const count = currentItems().filter(canImport).length;
    readyButton.disabled = state.busy || count === 0;
    readyButton.textContent = count ? `Import ready (${count})` : 'Import ready';
  }

  // ------------------------------------------------------------ match panel
  function matchSummary(entry) {
    if (entry._match) {
      const match = entry._match;
      const wrap = element('div', 'import-match-card');
      const poster = safeImage(match.poster);
      if (poster) {
        const image = element('img', 'import-match-poster');
        image.src = poster;
        image.alt = '';
        image.loading = 'lazy';
        wrap.append(image);
      }
      const info = element('div', 'import-match-text');
      info.append(element('strong', '', match.title || 'Untitled'));
      const meta = [];
      if (match.year) meta.push(String(match.year));
      if (match.original_title && match.original_title !== match.title) {
        meta.push(match.original_title);
      }
      if (meta.length) info.append(element('span', 'muted', meta.join(' · ')));
      if (match.overview) {
        info.append(element('p', 'import-overview', match.overview));
      }
      const badges = element('div', 'import-badges');
      badges.append(element('span', 'badge badge-monitored', 'TMDB match'));
      if (match.in_library) {
        const title = match.library_title ? ` in library: ${match.library_title}` : ' in library';
        badges.append(element('span', 'badge badge-imported', title.trim()));
      }
      info.append(badges);
      wrap.append(info);
      return wrap;
    }
    if (hasMatch(entry)) {
      const wrap = element('div', 'import-match-card import-local-match');
      const label = kindFor(entry) === 'movie'
        ? `Existing library movie #${entry.matched_movie_id}`
        : `Existing library series #${entry.matched_series_id}`;
      const info = element('div', 'import-match-text');
      info.append(element('strong', '', entry.matched_title || label));
      info.append(element('span', 'muted',
        'The file will be attached to this existing item. Choose another match to replace it.'));
      const badges = element('div', 'import-badges');
      badges.append(element('span', 'badge badge-monitored', 'Library match'));
      info.append(badges);
      wrap.append(info);
      return wrap;
    }
    const hint = element('p', 'hint',
      state.tmdbConfigured
        ? 'Fetch metadata to search TMDB with the parsed title, or type a different title.'
        : 'TMDB is not configured, so no new metadata can be fetched.');
    return hint;
  }

  function updateCard(entry, card) {
    const badges = card.querySelector('.import-badges');
    badges.replaceChildren();
    if (entry.resolution && entry.resolution !== 'unknown') {
      badges.append(element('span', 'badge badge-muted', entry.resolution));
    }
    if (entry._imported) {
      badges.append(element('span', 'badge badge-imported', 'Imported'));
    } else if (hasMatch(entry)) {
      badges.append(element('span', 'badge badge-monitored', 'Ready'));
    }
    if (entry.importable === false) {
      badges.append(element('span', 'badge badge-failed', 'Needs attention'));
    }
    if (entry._match?.in_library) {
      badges.append(element('span', 'badge badge-muted', 'Already in library'));
    }

    const matchBox = card.querySelector('.import-match-info');
    matchBox.replaceChildren(matchSummary(entry));

    const findButton = card.querySelector('.import-find');
    findButton.disabled = state.busy || entry._imported || !state.tmdbConfigured;
    findButton.textContent = entry._match ? 'Change metadata'
      : hasMatch(entry) ? 'Replace match' : 'Find metadata';

    const importButton = card.querySelector('.import-do');
    importButton.disabled = state.busy || !canImport(entry);
    importButton.textContent = entry._imported ? 'Imported' : 'Import';

    const message = card.querySelector('.import-message');
    message.textContent = entry._imported
      ? 'Attached to the library; no file was moved.'
      : (entry._error || entry.message || '');
    message.classList.toggle('is-bad', Boolean(entry._error));
    card.classList.toggle('import-card-done', Boolean(entry._imported));
    card.classList.toggle('import-card-warning', entry.importable === false);
  }

  // -------------------------------------------------------------- metadata
  function renderResults(entry, card, results) {
    const box = card.querySelector('.import-results');
    box.replaceChildren();
    if (!results.length) {
      box.append(element('p', 'hint', 'No TMDB result. Try a different title or year.'));
      return;
    }
    const buttons = [];
    const select = (result) => {
      entry._match = result;
      entry._error = '';
      buttons.forEach((other) => {
        other.setAttribute('aria-selected', other._result === result ? 'true' : 'false');
      });
      card.querySelector('.import-search').hidden = true;
      updateCard(entry, card);
      updateReadyButton();
      setStatus(status, `Selected “${result.title}”.`, 'ok');
    };
    results.forEach((result, index) => {
      const button = element('button', 'import-result');
      button.type = 'button';
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
      const poster = safeImage(result.poster);
      if (poster) {
        const image = element('img', 'import-result-poster');
        image.src = poster;
        image.alt = '';
        image.loading = 'lazy';
        button.append(image);
      }
      const body = element('span', 'import-result-body');
      body.append(element('strong', '', result.title || 'Untitled'));
      const meta = [];
      if (result.year) meta.push(String(result.year));
      if (result.original_title && result.original_title !== result.title) {
        meta.push(result.original_title);
      }
      if (meta.length) body.append(element('span', 'muted', meta.join(' · ')));
      if (result.in_library) {
        const label = result.library_title
          ? `Already in library: ${result.library_title}`
          : 'Already in library';
        body.append(element('span', 'badge badge-muted', label));
      }
      button.append(body);
      button._result = result;
      button.addEventListener('click', () => select(result));
      buttons.push(button);
      box.append(button);
    });
  }

  async function runSearch(entry, card) {
    const input = card.querySelector('.import-search-input');
    const query = (input?.value || '').trim();
    if (!query) {
      setStatus(status, 'Type a title to search.', 'bad');
      input?.focus();
      return;
    }
    const button = card.querySelector('.import-search-run');
    button.disabled = true;
    button.textContent = 'Searching…';
    setStatus(status, `Searching TMDB for “${query}”…`);
    try {
      // Do not filter by the parsed year: a typo or a localised title often
      // carries the wrong year, and TMDB's ranking is good enough to let the
      // user pick the right result.
      const params = new URLSearchParams({
        kind: kindFor(entry), q: query,
      });
      const data = await get(`/api/import/metadata/search?${params.toString()}`);
      const results = data.results || [];
      renderResults(entry, card, results);
      if (results.length) {
        // Preselect the first (most relevant) result, and let the user pick
        // another from the list. Fetching was explicit, so this is not a
        // silent automatic match.
        entry._match = results[0];
        entry._error = '';
        updateCard(entry, card);
        setStatus(status,
          `Found ${results.length} result${results.length === 1 ? '' : 's'}; ` +
          `selected “${results[0].title}”. Choose another if it is wrong.`, 'ok');
      } else {
        setStatus(status, 'No TMDB result for that title.', 'bad');
      }
      updateReadyButton();
    } catch (error) {
      setStatus(status, error.message, 'bad');
    } finally {
      button.disabled = false;
      button.textContent = 'Search';
    }
  }

  function openSearch(entry, card) {
    const search = card.querySelector('.import-search');
    search.hidden = false;
    const input = card.querySelector('.import-search-input');
    input.value = entry.title || '';
    input.focus();
    input.select();
  }

  // ---------------------------------------------------------------- import
  function payloadFor(entry) {
    const isEpisode = entry.kind === 'episode';
    return {
      kind: entry.kind,
      path: entry.path,
      tmdb_id: entry._match ? entry._match.tmdb_id : null,
      matched_movie_id: !entry._match ? (entry.matched_movie_id || null) : null,
      matched_series_id: !entry._match ? (entry.matched_series_id || null) : null,
      title: entry._match?.title || entry.title,
      year: entry._match?.year ?? entry.year,
      season: entry.season,
      episode: entry.episode,
      resolution: entry.resolution,
      quality_profile_id: state.profileId,
      series_mode: isEpisode ? state.seriesMode : null,
      seasons: isEpisode && state.seriesMode === 'manual' ? [...state.seasons] : [],
    };
  }

  async function importOne(entry, card) {
    if (!canImport(entry)) return false;
    entry._error = '';
    state.busy = true;
    updateCard(entry, card);
    updateReadyButton();
    setStatus(status, `Importing “${entry.title || entry.name}”…`);
    try {
      const data = await post('/api/import/confirm', {
        items: [payloadFor(entry)],
        quality_profile_id: state.profileId,
        series_mode: state.seriesMode,
        seasons: state.seriesMode === 'manual' ? [...state.seasons] : [],
      });
      entry._imported = true;
      entry._match = null;
      const item = data.item || (data.imported || [])[0] || {};
      setStatus(status,
        item.already_imported
          ? `“${item.title || entry.title}” was already imported.`
          : `Imported “${item.title || entry.title}”.`, 'ok');
      return true;
    } catch (error) {
      entry._error = error.message;
      setStatus(status, error.message, 'bad');
      return false;
    } finally {
      state.busy = false;
      updateCard(entry, card);
      updateReadyButton();
    }
  }

  // -------------------------------------------------------------- rendering
  function cardFor(entry) {
    const card = rowTemplate.content.firstElementChild.cloneNode(true);
    entry._card = card;
    card.dataset.path = entry.path;
    card.querySelector('.import-file-name').textContent = entry.name;
    card.querySelector('.import-file-path').textContent =
      `${entry.relative} · ${formatBytes(entry.size)}`;
    card.querySelector('.import-file-path').title = entry.path;
    card.querySelector('.import-parsed').textContent = parsedLabel(entry);

    card.querySelector('.import-find').addEventListener('click', () => {
      if (state.tmdbConfigured) openSearch(entry, card);
    });
    card.querySelector('.import-search-run').addEventListener('click', () => {
      runSearch(entry, card);
    });
    card.querySelector('.import-search-input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runSearch(entry, card);
      }
    });
    card.querySelector('.import-do').addEventListener('click', () => {
      importOne(entry, card);
    });
    updateCard(entry, card);
    return card;
  }

  function renderSeasons() {
    if (!seasonList || !seasonsBox) return;
    const seasons = new Set();
    state.items.series.forEach((entry) => {
      if (entry.season !== null && entry.season !== undefined) {
        seasons.add(Number(entry.season));
      }
    });
    const available = [...seasons].sort((a, b) => a - b);
    for (const value of [...state.seasons]) {
      if (!available.includes(value)) state.seasons.delete(value);
    }
    seasonList.replaceChildren();
    available.forEach((season) => {
      const label = element('label', 'check import-season');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = String(season);
      input.checked = state.seasons.has(season);
      input.addEventListener('change', () => {
        if (input.checked) state.seasons.add(season);
        else state.seasons.delete(season);
        updateReadyButton();
        currentItems().forEach((entry) => {
          if (entry._card) updateCard(entry, entry._card);
        });
      });
      label.append(input, document.createTextNode(` Season ${season}`));
      seasonList.append(label);
    });
    seasonsBox.hidden = state.seriesMode !== 'manual' || available.length === 0;
    if (state.seriesMode === 'manual' && available.length === 0) {
      seasonList.append(element('span', 'hint', 'No season numbers were parsed.'));
    }
  }

  function render() {
    const items = currentItems();
    list.replaceChildren(...items.map(cardFor));
    const visible = items.length > 0;
    list.hidden = !visible;
    empty.hidden = visible;
    if (!visible) {
      if (!state.loaded[state.tab]) {
        emptyTitle.textContent = 'Scan your folders';
        emptyText.textContent = 'Untracked movie and episode files appear here '
          + 'with the title, year and episode parsed from their names.';
      } else {
        emptyTitle.textContent = 'Nothing to import';
        emptyText.textContent = state.tab === 'movie'
          ? 'No untracked movie files were found in the Movies folder.'
          : 'No untracked episode files were found in the Series folder. '
            + 'Files already in the library are not listed.';
      }
    }
    seriesOptions.hidden = state.tab !== 'series';
    renderSeasons();
    updateReadyButton();
  }

  // ------------------------------------------------------------------ scan
  async function scan() {
    if (state.busy) return;
    state.busy = true;
    scanButton.disabled = true;
    scanButton.textContent = 'Scanning…';
    setStatus(status, `Scanning ${state.tab === 'movie' ? 'Movies' : 'Series'}…`);
    try {
      const data = await get(`/api/import/scan?kind=${state.tab}`);
      state.items[state.tab] = data.items || [];
      state.loaded[state.tab] = true;
      render();
      const count = state.items[state.tab].length;
      setStatus(status, count
        ? `${count} untracked file${count === 1 ? '' : 's'} found.`
        : 'No untracked files found.', 'ok');
    } catch (error) {
      setStatus(status, error.message, 'bad');
    } finally {
      state.busy = false;
      scanButton.disabled = false;
      scanButton.textContent = 'Scan folders';
      updateReadyButton();
    }
  }

  async function importReady() {
    const pending = currentItems().filter(canImport);
    if (!pending.length) return;
    let imported = 0;
    for (const entry of pending) {
      if (entry._card && await importOne(entry, entry._card)) imported += 1;
    }
    setStatus(status,
      `Imported ${imported} of ${pending.length} file${pending.length === 1 ? '' : 's'}.`,
      imported === pending.length ? 'ok' : 'bad');
  }

  function activateTab(tab) {
    if (tab !== 'movie' && tab !== 'series') return;
    state.tab = tab;
    document.querySelectorAll('.import-tab').forEach((button) => {
      button.setAttribute('aria-selected', button.dataset.tab === tab ? 'true' : 'false');
    });
    render();
    if (!state.loaded[tab]) scan();
  }

  // --------------------------------------------------------------- listeners
  document.querySelectorAll('.import-tab').forEach((button) => {
    button.addEventListener('click', () => activateTab(button.dataset.tab));
  });
  scanButton?.addEventListener('click', scan);
  readyButton?.addEventListener('click', importReady);
  profileSelect?.addEventListener('change', () => {
    state.profileId = Number(profileSelect.value) || null;
  });
  seriesMode?.addEventListener('change', () => {
    state.seriesMode = seriesMode.value;
    renderSeasons();
    currentItems().forEach((entry) => {
      if (entry._card) updateCard(entry, entry._card);
    });
    updateReadyButton();
  });

  activateTab(state.tab);
})();
