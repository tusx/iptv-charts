/* Dedicated Add Movie / Add Series pages: TMDB search, options and added state. */

(() => {
  'use strict';

  const { $, get, post, setStatus, debounce, element, imageUrl } = Manager;

  const moviePage = $('#add-movie-page');
  const seriesPage = $('#add-series-page');
  const page = moviePage || seriesPage;
  if (!page) return;

  const isMovie = Boolean(moviePage);
  const prefix = isMovie ? 'add-movie' : 'add-series';
  const form = $(`#${prefix}-search-form`);
  const query = $(`#${prefix}-query`);
  const searchButton = $(`#${prefix}-search-btn`);
  const status = $(`#${prefix}-status`);
  const results = $(`#${prefix}-results`);
  const profile = $(`#${prefix}-profile`);
  const monitored = $(`#${prefix}-monitored`);
  const searchNow = $(`#${prefix}-search-now`);
  const priority = $(`#${prefix}-priority`);
  const availability = isMovie ? $('#add-movie-availability') : null;
  const monitorMode = isMovie ? null : $('#add-series-monitor-mode');
  const newSeasons = isMovie ? null : $('#add-series-new-seasons');
  const searchUrl = page.dataset.searchUrl;
  const addUrl = page.dataset.addUrl;
  const detailBase = page.dataset.detailBase;
  let token = 0;

  const spinner = () => {
    const icon = element('i', 'fa-solid fa-spinner fa-spin');
    icon.setAttribute('aria-hidden', 'true');
    return icon;
  };

  function setBusy(busy, message) {
    if (searchButton) searchButton.disabled = busy;
    page.setAttribute('aria-busy', busy ? 'true' : 'false');
    if (message !== undefined) setStatus(status, message, busy ? '' : undefined);
    if (busy) {
      setStatus(status, '');
      status.replaceChildren(spinner(), document.createTextNode(` ${message || 'Searching TMDB…'}`));
    }
  }

  async function search() {
    const text = query.value.trim();
    if (text.length < 2) {
      results.replaceChildren();
      setStatus(status, 'Type at least two characters to search.', '');
      return;
    }
    const request = ++token;
    results.replaceChildren();
    setBusy(true, 'Searching TMDB…');
    try {
      const data = await get(`${searchUrl}?q=${encodeURIComponent(text)}`);
      if (request !== token) return;
      setBusy(false, '');
      if (!data.results.length) {
        setStatus(status, 'No matches on TMDB.', '');
        return;
      }
      setStatus(status,
        `${data.results.length} result${data.results.length === 1 ? '' : 's'} — choose one to add.`);
      data.results.forEach((item) => results.append(renderResult(item)));
    } catch (error) {
      if (request === token) {
        setBusy(false, '');
        setStatus(status, error.message, 'bad');
      }
    }
  }

  function payloadFor(item) {
    const base = {
      tmdb_id: item.tmdb_id,
      quality_profile_id: Number(profile?.value) || null,
      monitored: monitored ? monitored.checked : true,
      search: searchNow ? searchNow.checked : true,
      priority: priority ? Number(priority.value) || 0 : 0,
    };
    if (isMovie) {
      base.minimum_availability = availability ? availability.value : 'released';
    } else {
      base.monitor_mode = monitorMode ? monitorMode.value : 'all';
      base.monitor_new_seasons = newSeasons ? newSeasons.value : 'all';
    }
    return base;
  }

  function renderResult(item) {
    const row = element('article', 'add-result');
    const poster = element('span', 'add-poster');
    const image = imageUrl('w92', item.poster_path);
    if (image) {
      const img = document.createElement('img');
      img.src = image;
      img.alt = '';
      img.loading = 'lazy';
      poster.append(img);
    } else {
      poster.append(element('span', 'poster-fallback', (item.title || '?')[0]));
    }

    const body = element('div', 'add-result-body');
    body.append(element('strong', '', item.title));
    body.append(element('span', 'muted',
      `${item.year || 'Year unknown'}${item.rating ? ` · ★ ${Number(item.rating).toFixed(1)}` : ''}`));
    if (item.overview) body.append(element('p', 'metadata-overview', item.overview));

    const add = element('button', 'btn btn-primary btn-sm', 'Add');
    add.type = 'button';
    add.addEventListener('click', async () => {
      if (!profile || !profile.value) {
        setStatus(status, 'Create a quality profile first.', 'bad');
        return;
      }
      add.disabled = true;
      add.textContent = 'Adding…';
      setStatus(status, searchNow?.checked
        ? 'Adding the title and starting its first search…'
        : 'Adding the title…');
      try {
        const data = await post(addUrl, payloadFor(item));
        const added = isMovie ? data.movie : data.series;
        showAdded(added || item);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        add.disabled = false;
        add.textContent = 'Add';
      }
    });

    row.append(poster, body, add);
    return row;
  }

  function showAdded(item) {
    const title = item.title || item.name || 'Title';
    const id = item.id;
    const card = element('section', 'add-success');
    const badge = element('span', 'badge badge-downloaded', 'Added to library');
    const heading = element('h2', '', title);
    const copy = element('p', 'muted',
      isMovie
        ? 'The movie is in your library. The selected profile and availability now apply.'
        : 'The series is in your library. The selected episode monitoring rules now apply.');
    const actions = element('div', 'add-success-actions');
    if (id !== undefined && id !== null) {
      const open = element('a', 'btn btn-primary',
        isMovie ? 'Open movie' : 'Open series');
      open.href = `${detailBase}${id}`;
      actions.append(open);
    }
    const another = element('button', 'btn btn-ghost', 'Add another');
    another.type = 'button';
    another.addEventListener('click', () => {
      results.replaceChildren();
      query.value = '';
      query.focus();
      setStatus(status, 'Type a title and press Search.');
    });
    actions.append(another);
    card.append(badge, heading, copy, actions);
    results.replaceChildren(card);
    setStatus(status, `${title} was added to your library.`, 'ok');
  }

  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    search();
  });
  const debounced = debounce(search, 450);
  query?.addEventListener('input', () => {
    if (query.value.trim().length >= 2) {
      debounced();
      return;
    }
    token += 1;
    results.replaceChildren();
    setStatus(status, 'Type at least two characters to search.');
  });
})();
