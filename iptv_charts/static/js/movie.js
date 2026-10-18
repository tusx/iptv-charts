/* Movie detail page: actions, editing, manual search and deletion. */

(() => {
  'use strict';

  const {
    $, get, post, patch, del, setStatus, debounce, element, open, close, feedback,
  } = Manager;

  const movieId = window.MOVIE_ID;
  if (!movieId) return;

  const status = $('#movie-action-status');
  const releasesDialog = $('#movie-search-dialog');
  const releaseStatus = $('#movie-release-status');
  const releaseList = $('#movie-release-list');
  const releaseQuery = $('#movie-release-query');
  let releaseToken = 0;

  async function action(button, fn, message) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Working…';
    setStatus(status, '');
    try {
      const result = await fn();
      setStatus(status, result?.message || message, 'ok');
      if (result?.queued || result?.download) {
        window.setTimeout(() => window.location.reload(), 700);
      }
    } catch (error) {
      setStatus(status, error.message, 'bad');
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  $('#movie-search-now')?.addEventListener('click', (event) => {
    action(event.currentTarget, () => post(`/api/movies/${movieId}/search`), 'Search finished.');
  });

  $('#movie-refresh')?.addEventListener('click', (event) => {
    action(event.currentTarget, () => post(`/api/movies/${movieId}/refresh`), 'Metadata refreshed.');
  });

  $('#movie-edit-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await patch(`/api/movies/${movieId}`, {
        monitored: $('#movie-monitored-edit').checked,
        quality_profile_id: Number($('#movie-profile-edit').value),
        minimum_availability: $('#movie-availability-edit').value,
        priority: Number($('#movie-priority-edit').value) || 0,
      });
      setStatus($('#movie-edit-status'), 'Saved.', 'ok');
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      setStatus($('#movie-edit-status'), error.message, 'bad');
    } finally {
      button.disabled = false;
    }
  });

  $('[data-cancel-download]')?.addEventListener('click', async (event) => {
    try {
      await post(`/api/downloads/${event.currentTarget.dataset.cancelDownload}/cancel`);
      feedback(event.currentTarget, 'Cancelling');
      window.setTimeout(() => window.location.reload(), 600);
    } catch (error) {
      setStatus(status, error.message, 'bad');
    }
  });

  // ------------------------------------------------------------ manual search
  $('#movie-manual-search')?.addEventListener('click', () => {
    releaseQuery.value = '';
    open(releasesDialog);
    loadReleases('');
  });

  const debouncedLoad = debounce(() => loadReleases(releaseQuery.value.trim()), 300);
  releaseQuery.addEventListener('input', debouncedLoad);

  async function loadReleases(query) {
    const request = ++releaseToken;
    releaseList.replaceChildren(element('p', 'muted', 'Searching your playlists…'));
    setStatus(releaseStatus, '');
    try {
      const params = query ? `?q=${encodeURIComponent(query)}` : '';
      const data = await get(`/api/movies/${movieId}/releases${params}`);
      if (request !== releaseToken) return;
      releaseList.replaceChildren();
      if (!data.releases.length) {
        releaseList.append(element('p', 'muted', 'No matching releases found. Try a different search.'));
        return;
      }
      data.releases.forEach((release) => releaseList.append(renderRelease(release)));
    } catch (error) {
      if (request === releaseToken) {
        releaseList.replaceChildren();
        setStatus(releaseStatus, error.message, 'bad');
      }
    }
  }

  function renderRelease(release) {
    const row = element('div', 'release-row');
    const main = element('div', 'release-main');
    const probe = release.probe || {};
    main.append(element('strong', '', release.name));
    const bits = [
      release.source_name || 'Unknown playlist',
      release.candidate_year || 'year ?',
      release.resolution || 'unknown',
      release.source && release.source !== 'unknown' ? release.source.toUpperCase() : null,
      release.quality_label || null,
    ].filter(Boolean);
    main.append(element('span', 'muted', bits.join(' · ')));
    if (probe.ok) {
      const tracks = `${probe.audio_track_count || 0} audio · ${probe.subtitle_track_count || 0} subtitle`;
      const langs = [];
      if ((release.audio_languages || []).length) langs.push(`audio ${release.audio_languages.join('/')}`);
      if ((release.subtitle_languages || []).length) langs.push(`subs ${release.subtitle_languages.join('/')}`);
      main.append(element('span', 'muted small',
        `${probe.video_codec || 'video'}${probe.hdr && probe.hdr !== 'SDR' ? ` ${probe.hdr}` : ''} · ${tracks}${langs.length ? ` · ${langs.join(' · ')}` : ''}`));
    } else if (probe.error) {
      main.append(element('span', 'queue-error small', `Probe failed: ${probe.error}`));
    }
    if (release.reason) {
      main.append(element('span', release.allowed ? 'muted small' : 'queue-error small', release.reason));
    }
    main.append(element('span', 'release-score', `match ${Math.round((release.score || 0) * 100)}%`));

    const badge = element('span', `badge ${release.allowed ? 'badge-downloaded' : 'badge-missing'}`,
      release.allowed ? 'Profile allows' : 'Outside profile');
    const grab = element('button', 'btn btn-primary btn-sm', 'Grab');
    grab.type = 'button';
    grab.disabled = !release.allowed;
    if (!release.allowed) grab.title = 'This release does not satisfy the quality profile or stream probe.';
    grab.addEventListener('click', async () => {
      grab.disabled = true;
      grab.textContent = 'Queuing…';
      try {
        await post(`/api/movies/${movieId}/grab`, {
          source_id: release.source_id,
          remote_id: release.remote_id,
        });
        setStatus(releaseStatus, 'Download queued.', 'ok');
        close(releasesDialog);
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        setStatus(releaseStatus, error.message, 'bad');
        grab.disabled = false;
        grab.textContent = 'Grab';
      }
    });

    row.append(main, badge, grab);
    return row;
  }

  // ----------------------------------------------------------------- delete
  $('#movie-delete')?.addEventListener('click', () => open($('#movie-delete-dialog')));
  $('#movie-delete-confirm')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await del(`/api/movies/${movieId}`, {
        delete_files: $('#movie-delete-files').checked,
      });
      window.location.href = '/movies';
    } catch (error) {
      setStatus(status, error.message, 'bad');
      button.disabled = false;
    }
  });
})();
