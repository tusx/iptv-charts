/* Series detail: monitoring, searches, manual episode search and deletion. */

(() => {
  'use strict';

  const {
    $, get, post, patch, del, setStatus, debounce, element, open, close, feedback,
  } = Manager;

  const seriesId = window.SERIES_ID;
  if (!seriesId) return;

  const status = $('#series-action-status');
  const dialog = $('#episode-search-dialog');
  const releaseList = $('#episode-release-list');
  const releaseStatus = $('#episode-release-status');
  const releaseQuery = $('#episode-release-query');
  let targetEpisode = null;
  let releaseToken = 0;

  $('#series-search-now')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Searching…';
    setStatus(status, '');
    try {
      const result = await post(`/api/series/${seriesId}/search`);
      setStatus(status, result.message || 'Search finished.', 'ok');
      if (result.queued) window.setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      setStatus(status, error.message, 'bad');
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });

  $('#series-refresh')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    setStatus(status, 'Refreshing metadata…');
    try {
      await post(`/api/series/${seriesId}/refresh`);
      setStatus(status, 'Metadata refreshed.', 'ok');
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      setStatus(status, error.message, 'bad');
      button.disabled = false;
    }
  });

  $('#series-edit-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await patch(`/api/series/${seriesId}`, {
        monitored: $('#series-monitored-edit').checked,
        quality_profile_id: Number($('#series-profile-edit').value),
      });
      setStatus($('#series-edit-status'), 'Saved.', 'ok');
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      setStatus($('#series-edit-status'), error.message, 'bad');
    } finally {
      button.disabled = false;
    }
  });

  // ------------------------------------------------------------- monitoring
  document.addEventListener('change', async (event) => {
    const episodeInput = event.target.closest('[data-episode-monitor]');
    if (episodeInput) {
      const previous = !episodeInput.checked;
      try {
        await patch(`/api/series/${seriesId}/episodes/${episodeInput.dataset.episodeMonitor}`,
          { monitored: episodeInput.checked });
        setStatus(status, episodeInput.checked ? 'Episode monitored.' : 'Episode unmonitored.', 'ok');
      } catch (error) {
        episodeInput.checked = previous;
        setStatus(status, error.message, 'bad');
      }
      return;
    }

    const seasonInput = event.target.closest('[data-season-monitor]');
    if (seasonInput) {
      const previous = !seasonInput.checked;
      seasonInput.disabled = true;
      try {
        await patch(
          `/api/series/${seriesId}/seasons/${seasonInput.dataset.seasonMonitor}/monitor`,
          { monitored: seasonInput.checked },
        );
        const block = seasonInput.closest('details');
        block?.querySelectorAll('[data-episode-monitor]').forEach((input) => {
          input.checked = seasonInput.checked;
        });
        setStatus(status, seasonInput.checked ? 'Season monitored.' : 'Season unmonitored.', 'ok');
      } catch (error) {
        seasonInput.checked = previous;
        setStatus(status, error.message, 'bad');
      } finally {
        seasonInput.disabled = false;
      }
    }
  });

  // ------------------------------------------------------------ episode tasks
  document.addEventListener('click', async (event) => {
    const searchButton = event.target.closest('[data-episode-search]');
    if (searchButton) {
      searchButton.disabled = true;
      try {
        const result = await post(
          `/api/series/${seriesId}/episodes/${searchButton.dataset.episodeSearch}/search`,
        );
        setStatus(status, result.message || 'Search finished.', 'ok');
        if (result.queued) window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        setStatus(status, error.message, 'bad');
      } finally {
        searchButton.disabled = false;
      }
      return;
    }

    const manualButton = event.target.closest('[data-episode-manual]');
    if (manualButton) {
      targetEpisode = manualButton.dataset.episodeManual;
      releaseQuery.value = '';
      open(dialog);
      loadReleases('');
    }
  });

  // ------------------------------------------------------------ manual search
  const debouncedLoad = debounce(() => loadReleases(releaseQuery.value.trim()), 300);
  releaseQuery.addEventListener('input', debouncedLoad);

  async function loadReleases(query) {
    if (!targetEpisode) return;
    const request = ++releaseToken;
    releaseList.replaceChildren(element('p', 'muted', 'Searching your playlists…'));
    setStatus(releaseStatus, '');
    try {
      const params = query ? `?q=${encodeURIComponent(query)}` : '';
      const data = await get(
        `/api/series/${seriesId}/episodes/${targetEpisode}/releases${params}`,
      );
      if (request !== releaseToken) return;
      releaseList.replaceChildren();
      if (!data.releases.length) {
        releaseList.append(element('p', 'muted', 'No matching provider episode found. Try a different search.'));
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
    main.append(element('strong', '', release.name));
    main.append(element('span', 'muted',
      `${release.source_name || 'Unknown playlist'} · ${release.resolution || 'unknown'} · ` +
      `S${String(release.provider_episode.season).padStart(2, '0')}E${String(release.provider_episode.episode).padStart(2, '0')}`));
    main.append(element('span', 'release-score', `match ${Math.round((release.score || 0) * 100)}%`));
    const badge = element('span', `badge ${release.allowed ? 'badge-downloaded' : 'badge-missing'}`,
      release.allowed ? 'Profile allows' : 'Outside profile');
    const grab = element('button', 'btn btn-primary btn-sm', 'Grab');
    grab.type = 'button';
    grab.addEventListener('click', async () => {
      grab.disabled = true;
      grab.textContent = 'Queuing…';
      try {
        await post(
          `/api/series/${seriesId}/episodes/${targetEpisode}/grab`,
          {
            source_id: release.source_id,
            provider_remote_id: release.remote_id,
            stream_id: release.provider_episode.stream_id,
          },
        );
        setStatus(releaseStatus, 'Download queued.', 'ok');
        close(dialog);
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
  $('#series-delete')?.addEventListener('click', () => open($('#series-delete-dialog')));
  $('#series-delete-confirm')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await del(`/api/series/${seriesId}`, {
        delete_files: $('#series-delete-files').checked,
      });
      window.location.href = '/series';
    } catch (error) {
      setStatus(status, error.message, 'bad');
      button.disabled = false;
    }
  });
})();
