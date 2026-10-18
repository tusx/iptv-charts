/* Movies index: TMDB search, add dialog, and the "search missing" task. */

(() => {
  'use strict';

  const {
    $, get, post, setStatus, debounce, element, imageUrl, open, feedback,
  } = Manager;

  const dialog = $('#add-movie-dialog');
  if (!dialog) return;

  const input = $('#movie-search-input');
  const results = $('#movie-search-results');
  const status = $('#movie-search-status');
  const profile = $('#movie-profile');
  const monitored = $('#movie-monitored');
  const searchNow = $('#movie-search-now');
  const availability = $('#movie-availability');
  const priority = $('#movie-priority');
  let token = 0;

  $('#add-movie-btn')?.addEventListener('click', () => open(dialog));
  if (new URLSearchParams(window.location.search).get('add') === '1') {
    open(dialog);
    history.replaceState({}, '', window.location.pathname);
  }

  const search = debounce(async () => {
    const query = input.value.trim();
    const request = ++token;
    results.replaceChildren();
    if (!query) {
      setStatus(status, '');
      return;
    }
    setStatus(status, 'Searching TMDB…');
    try {
      const data = await get(`/api/metadata/movie/search?q=${encodeURIComponent(query)}`);
      if (request !== token) return;
      if (!data.results.length) {
        setStatus(status, 'No matches on TMDB.', 'bad');
        return;
      }
      setStatus(status, `${data.results.length} result${data.results.length === 1 ? '' : 's'} — choose one to add.`);
      data.results.forEach((item) => results.append(renderResult(item)));
    } catch (error) {
      if (request === token) setStatus(status, error.message, 'bad');
    }
  }, 300);

  input.addEventListener('input', search);

  dialog.addEventListener('close', () => {
    token += 1;
    input.value = '';
    results.replaceChildren();
    setStatus(status, '');
  });

  function renderResult(item) {
    const row = element('div', 'metadata-result');
    const poster = element('span', 'metadata-poster');
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

    const body = element('div', 'metadata-result-body');
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
      try {
        await post('/api/movies', {
          tmdb_id: item.tmdb_id,
          quality_profile_id: Number(profile.value),
          monitored: monitored ? monitored.checked : true,
          search: searchNow ? searchNow.checked : true,
          minimum_availability: availability ? availability.value : 'released',
          priority: priority ? Number(priority.value) || 0 : 0,
        });
        setStatus(status, 'Added — loading the library…', 'ok');
        window.setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        add.disabled = false;
        add.textContent = 'Add';
      }
    });

    row.append(poster, body, add);
    return row;
  }

  $('#search-missing-btn')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    try {
      await post('/api/tasks/search_missing/run');
      feedback(button, 'Search started');
    } catch (error) {
      feedback(button, error.message, false);
    }
  });
})();
