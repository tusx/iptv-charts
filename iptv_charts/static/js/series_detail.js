/* Series detail: bworker stage/history, episode controls, metadata and files. */

(() => {
  'use strict';

  const {
    $, $$, get, post, patch, del, setStatus, debounce, element, open, close,
    mediaFacts,
  } = Manager;

  const seriesId = window.SERIES_ID;
  const seriesTitle = window.SERIES_TITLE || '';
  if (!seriesId) return;

  const status = $('#series-action-status');
  const releasesDialog = $('#episode-search-dialog');
  const releaseList = $('#episode-release-list');
  const releaseStatus = $('#episode-release-status');
  const releaseQuery = $('#episode-release-query');
  const allCandidatesButton = $('#episode-all-candidates');
  let targetEpisode = null;
  let releaseToken = 0;
  let allCandidatesMode = false;

  // ------------------------------------------------------- stage / history
  // Both panels are server-rendered. Switching tabs only reveals markup that
  // is already on the page, so it never shows an empty state while fetching.
  const stageTabs = $$('[data-stage-tab]');
  const stagePanels = $$('[data-stage-panel]');

  function activateStageTab(name, focus = false) {
    stageTabs.forEach((tab) => {
      const active = tab.dataset.stageTab === name;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });
    stagePanels.forEach((panel) => {
      panel.hidden = panel.dataset.stagePanel !== name;
    });
  }

  stageTabs.forEach((tab) => {
    tab.addEventListener('click', () => activateStageTab(tab.dataset.stageTab));
    tab.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const index = stageTabs.indexOf(tab);
      const next = event.key === 'ArrowRight'
        ? stageTabs[(index + 1) % stageTabs.length]
        : stageTabs[(index - 1 + stageTabs.length) % stageTabs.length];
      activateStageTab(next.dataset.stageTab, true);
    });
  });
  if (window.location.hash === '#series-stage-history') {
    activateStageTab('history');
  }

  $('#series-stage-refresh')?.addEventListener('click', () => window.location.reload());

  // A04's /api/series/<id>/stage is present on the release branch. Poll it and
  // reload only when the aggregate stage, status, message, score, episode
  // progress or newest event actually changed. If the route is absent, the
  // server render plus action reloads remain the refresh path.
  if (window.SERIES_STAGE_URL) {
    let signature = window.SERIES_STAGE_SIGNATURE || '';
    const stageSignature = (data) => {
      if (!data || typeof data !== 'object') return '';
      const context = typeof data.stage === 'string'
        ? data
        : (data.stage_context || data.context || data.data || {});
      const events = context.events || [];
      const progress = context.progress || {};
      const done = progress.done ?? '';
      const total = progress.total ?? '';
      return [
        String(context.stage || 'pending'),
        String(context.status || ''),
        String(context.message || ''),
        context.best_score == null ? '' : String(context.best_score),
        `${done}/${total}`,
        events.length ? String(events[0].id ?? '') : '',
      ].join('|');
    };
    window.setInterval(async () => {
      if (document.hidden) return;
      try {
        const data = await get(window.SERIES_STAGE_URL);
        const next = stageSignature(data);
        if (next && next !== signature) {
          signature = next;
          window.location.reload();
        }
      } catch (error) {
        // Keep the server render; a transient read error must never blank the
        // page or start a reload loop.
      }
    }, 15000);
  }

  async function action(button, fn, message, { reload = false } = {}) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Working…';
    setStatus(status, '');
    try {
      const result = await fn();
      setStatus(status, result?.message || message, 'ok');
      if (reload || result?.queued || result?.download) {
        window.setTimeout(() => window.location.reload(), 700);
      }
    } catch (error) {
      setStatus(status, error.message, 'bad');
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  $('#series-search-now')?.addEventListener('click', (event) => {
    action(event.currentTarget, () => post(`/api/series/${seriesId}/search`),
      'Search finished.', { reload: true });
  });

  $('#series-refresh')?.addEventListener('click', (event) => {
    action(event.currentTarget, () => post(`/api/series/${seriesId}/refresh`),
      'Metadata refreshed.', { reload: true });
  });

  $('#series-edit-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await patch(`/api/series/${seriesId}`, {
        monitored: $('#series-monitored-edit').checked,
        quality_profile_id: Number($('#series-profile-edit').value),
        monitor_new_seasons: $('#series-new-seasons-edit').value,
        priority: Number($('#series-priority-edit').value) || 0,
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
        const card = seasonInput.closest('.season-card');
        card?.querySelectorAll('[data-episode-monitor]').forEach((input) => {
          input.checked = seasonInput.checked;
        });
        setStatus(status, seasonInput.checked ? 'Season monitored.' : 'Season unmonitored.', 'ok');
        // Monitoring changes which work bworker considers; refresh the stages.
        window.setTimeout(() => window.location.reload(), 600);
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
      setAllCandidatesMode(false);
      open(releasesDialog);
      releaseQuery.focus();
      showSearchHint();
      return;
    }

    const allButton = event.target.closest('[data-episode-all]');
    if (allButton) {
      targetEpisode = allButton.dataset.episodeAll;
      releaseQuery.value = seriesTitle;
      setAllCandidatesMode(true);
      open(releasesDialog);
      loadReleases(seriesTitle);
      return;
    }

    const refindRow = event.target.closest('[data-episode-refind]');
    if (refindRow) {
      const episodeId = refindRow.dataset.episodeRefind;
      refindRow.disabled = true;
      setStatus(status, 'Resetting this episode’s candidate scan…');
      try {
        await post(
          `/api/series/${seriesId}/episodes/${episodeId}/refind`, {},
        );
        setStatus(status, 'Candidate scan reset; a fresh search is queued.', 'ok');
        window.setTimeout(() => window.location.reload(), 800);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        refindRow.disabled = false;
      }
      return;
    }

    const reprobeRow = event.target.closest('[data-episode-reprobe-all]');
    if (reprobeRow) {
      const episodeId = reprobeRow.dataset.episodeReprobeAll;
      reprobeRow.disabled = true;
      setStatus(status, 'Queueing ffprobe for every candidate of this episode…');
      try {
        const result = await post(
          `/api/series/${seriesId}/episodes/${episodeId}/candidates/probe-all`,
          { all: true, refresh: true },
        );
        setStatus(status,
          `Probed ${result.probed || 0} candidate(s); ${result.failed || 0} failed.`,
          'ok');
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        reprobeRow.disabled = false;
      }
      return;
    }

    const removeRefindRow = event.target.closest('[data-episode-remove-refind]');
    if (removeRefindRow) {
      if (!window.confirm(
        'Delete this episode file and search again? The episode metadata is kept; '
        + 'the file and candidate scan are rebuilt.',
      )) return;
      const episodeId = removeRefindRow.dataset.episodeRemoveRefind;
      removeRefindRow.disabled = true;
      setStatus(status, 'Removing the file and resetting the candidate scan…');
      try {
        await post(
          `/api/series/${seriesId}/episodes/${episodeId}/remove-and-refind`, {},
        );
        setStatus(status, 'File removed; a fresh episode search is queued.', 'ok');
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        removeRefindRow.disabled = false;
      }
      return;
    }

    // A season action is a loop over the existing per-episode search endpoint;
    // no separate season-search API exists. Only monitored episodes are
    // queued, matching bworker's own scope.
    const seasonSearch = event.target.closest('[data-season-search]');
    if (seasonSearch) {
      const card = seasonSearch.closest('.season-card');
      const ids = $$('[data-episode-monitor]', card)
        .filter((input) => input.checked)
        .map((input) => input.dataset.episodeMonitor);
      if (!ids.length) {
        setStatus(status, 'No monitored episodes in this season.', 'bad');
        return;
      }
      seasonSearch.disabled = true;
      setStatus(status, `Queueing searches for ${ids.length} monitored episode${ids.length === 1 ? '' : 's'}…`);
      let queued = 0;
      try {
        for (const episodeId of ids) {
          const result = await post(
            `/api/series/${seriesId}/episodes/${episodeId}/search`);
          if (result?.queued) queued += 1;
        }
        setStatus(status,
          `Search queued for ${queued} of ${ids.length} monitored episode${ids.length === 1 ? '' : 's'}.`,
          'ok');
        window.setTimeout(() => window.location.reload(), 1000);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        seasonSearch.disabled = false;
      }
      return;
    }

    // Failed work is retried through the bworker reset API, which is the only
    // supported way to clear a failed-stopped counter. This does not change
    // bworker semantics: it is the same reset exposed by the Activity page.
    const retryButton = event.target.closest('[data-episode-retry]');
    if (retryButton) {
      retryButton.disabled = true;
      try {
        const result = await post('/api/bworker/reset', {
          work_id: Number(retryButton.dataset.workId),
        });
        setStatus(status,
          `Reset ${result.updated || 0} bworker work unit${result.updated === 1 ? '' : 's'}.`,
          'ok');
        window.setTimeout(() => window.location.reload(), 800);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        retryButton.disabled = false;
      }
      return;
    }

    const removeButton = event.target.closest('[data-episode-remove-file]');
    if (removeButton) {
      openRemoveDialog(removeButton);
    }
  });

  // ------------------------------------------------------------ manual search
  function setAllCandidatesMode(enabled) {
    allCandidatesMode = enabled;
    if (allCandidatesButton) {
      allCandidatesButton.textContent = enabled
        ? 'Show search results'
        : 'Show all candidates';
      allCandidatesButton.classList.toggle('is-active', enabled);
    }
  }

  function releaseSpinner() {
    const icon = element('i', 'fa-solid fa-spinner fa-spin');
    icon.setAttribute('aria-hidden', 'true');
    return icon;
  }

  function showSearchHint() {
    releaseList.replaceChildren();
    setStatus(releaseStatus,
      'Type at least two characters. This searches all playlist items and may take some time.');
  }

  allCandidatesButton?.addEventListener('click', () => {
    const enabled = !allCandidatesMode;
    setAllCandidatesMode(enabled);
    if (enabled && releaseQuery.value.trim().length < 2) {
      releaseQuery.value = seriesTitle;
    }
    const query = releaseQuery.value.trim();
    if (query.length >= 2) loadReleases(query);
    else showSearchHint();
  });
  releaseQuery.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      const query = releaseQuery.value.trim();
      if (query.length >= 2) loadReleases(query);
      else showSearchHint();
    }
  });

  const debouncedLoad = debounce(() => {
    const query = releaseQuery.value.trim();
    if (query.length >= 2) loadReleases(query);
    else {
      releaseToken += 1;
      showSearchHint();
    }
  }, 300);
  releaseQuery.addEventListener('input', debouncedLoad);

  async function loadReleases(query) {
    if (!targetEpisode) return;
    if (!String(query || '').trim()) {
      showSearchHint();
      return;
    }
    const request = ++releaseToken;
    const manualMode = allCandidatesMode;
    const loading = element('p', 'muted');
    loading.append(
      releaseSpinner(),
      document.createTextNode(' Searching all playlist items — this can take some time…'),
    );
    releaseList.replaceChildren(loading);
    setStatus(releaseStatus,
      'Searching all playlist items. This can take some time; results appear when the search finishes.');
    try {
      const params = new URLSearchParams();
      if (query) params.set('q', query);
      if (manualMode) {
        params.set('all', '1');
        params.set('limit', '500');
      }
      const suffix = params.toString() ? `?${params.toString()}` : '';
      const data = await get(
        manualMode
          ? `/api/series/${seriesId}/episodes/${targetEpisode}/candidates${suffix}`
          : `/api/series/${seriesId}/episodes/${targetEpisode}/releases${suffix}`,
      );
      if (request !== releaseToken) return;
      const rows = manualMode ? (data.candidates || []) : (data.releases || []);
      releaseList.replaceChildren();
      if (!rows.length) {
        releaseList.append(element(
          'p', 'muted',
          manualMode
            ? 'No provider episode candidates matched. Try a shorter or different search.'
            : 'No matching provider episode found. Try a different search or “Show all candidates”.',
        ));
        return;
      }
      rows.forEach((release) => releaseList.append(renderRelease(release, manualMode)));
      const allowed = rows.filter((release) => release.allowed).length;
      setStatus(
        releaseStatus,
        `${rows.length} ${manualMode ? 'catalog candidate' : 'release'}${rows.length === 1 ? '' : 's'}`
        + ` · ${allowed} allowed by the profile`
        + (manualMode ? '. Download any candidate to override the profile.' : '.'),
      );
    } catch (error) {
      if (request === releaseToken) {
        releaseList.replaceChildren();
        setStatus(releaseStatus, error.message, 'bad');
      }
    }
  }

  function renderRelease(release, manualMode = false) {
    const row = element('div', 'release-row');
    const main = element('div', 'release-main');
    const probe = release.probe || {};
    const providerEpisode = release.provider_episode || {};
    main.append(element('strong', '', release.name));
    const bits = [
      release.source_name || 'Unknown playlist',
      release.resolution || 'unknown',
      release.source && release.source !== 'unknown' ? release.source.toUpperCase() : null,
      providerEpisode.season !== undefined
        ? `S${String(providerEpisode.season).padStart(2, '0')}E${String(providerEpisode.episode).padStart(2, '0')}`
        : null,
      release.quality_label || null,
    ].filter(Boolean);
    main.append(element('span', 'muted', bits.join(' · ')));
    if (probe.ok) {
      main.append(element('span', 'muted small',
        `${probe.audio_track_count || 0} audio track${probe.audio_track_count === 1 ? '' : 's'} · `
        + `${probe.subtitle_track_count || 0} subtitle track${probe.subtitle_track_count === 1 ? '' : 's'}`
        + `${probe.hdr && probe.hdr !== 'SDR' ? ` · ${probe.hdr}` : ''}`));
      main.append(mediaFacts(release));
    } else if (probe.error) {
      main.append(element('span', 'queue-error small', `Probe failed: ${probe.error}`));
    } else if (manualMode) {
      main.append(element('span', 'hint', 'Not probed yet. Probe it, or Download to probe and queue it.'));
    }
    if (release.reason) {
      main.append(element('span', release.allowed ? 'muted small' : 'queue-error small', release.reason));
    }
    main.append(element('span', 'release-score', `match ${Math.round((release.score || 0) * 100)}%`));
    const badge = element('span', `badge ${release.allowed ? 'badge-downloaded' : 'badge-missing'}`,
      release.allowed ? 'Profile allows' : 'Outside profile');
    const actions = element('div', 'release-actions');

    if (manualMode) {
      const probeButton = element(
        'button', 'btn btn-ghost btn-sm',
        release.probe_status === 'not_probed' ? 'Probe' : 'Re-probe',
      );
      probeButton.type = 'button';
      probeButton.addEventListener('click', async () => {
        probeButton.disabled = true;
        probeButton.textContent = 'Probing…';
        setStatus(releaseStatus, 'Running ffprobe on this candidate…');
        try {
          await post(
            `/api/series/${seriesId}/episodes/${targetEpisode}/candidates/probe`,
            {
              source_id: release.source_id,
              provider_remote_id: release.remote_id,
              stream_id: providerEpisode.stream_id,
              refresh: release.probe_status !== 'not_probed',
            },
          );
          setStatus(releaseStatus, 'Probe finished.', 'ok');
          await loadReleases(releaseQuery.value.trim());
        } catch (error) {
          setStatus(releaseStatus, error.message, 'bad');
          probeButton.disabled = false;
          probeButton.textContent = 'Probe';
        }
      });
      actions.append(probeButton);
    }

    const grab = element('button', 'btn btn-primary btn-sm',
      manualMode ? 'Download' : 'Grab');
    grab.type = 'button';
    if (!manualMode) {
      grab.disabled = !release.allowed;
      if (!release.allowed) grab.title = 'This release does not satisfy the quality profile or stream probe.';
    } else if (!release.allowed) {
      grab.title = 'Download this candidate manually, overriding the quality profile.';
    }
    grab.addEventListener('click', async () => {
      if (manualMode && !release.allowed && !window.confirm(
        'Download this candidate even though it is outside the quality profile? '
        + 'ffprobe runs first and the download is marked as a manual override.',
      )) return;
      grab.disabled = true;
      grab.textContent = 'Queuing…';
      try {
        await post(
          `/api/series/${seriesId}/episodes/${targetEpisode}/grab`,
          {
            source_id: release.source_id,
            provider_remote_id: release.remote_id,
            stream_id: providerEpisode.stream_id,
            force: manualMode && !release.allowed,
          },
        );
        setStatus(releaseStatus, 'Download queued.', 'ok');
        close(releasesDialog);
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        setStatus(releaseStatus, error.message, 'bad');
        grab.disabled = false;
        grab.textContent = manualMode ? 'Download' : 'Grab';
      }
    });
    actions.append(grab);

    row.append(main, badge, actions);
    return row;
  }

  async function episodeAction(button, url, body, message) {
    if (!targetEpisode) return;
    button.disabled = true;
    setStatus(releaseStatus, 'Working…');
    try {
      await post(url, body || {});
      setStatus(releaseStatus, message, 'ok');
      window.setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      setStatus(releaseStatus, error.message, 'bad');
      button.disabled = false;
    }
  }

  $('#episode-refind')?.addEventListener('click', (event) => {
    if (!targetEpisode) return;
    episodeAction(
      event.currentTarget,
      `/api/series/${seriesId}/episodes/${targetEpisode}/refind`,
      {},
      'Candidate scan reset; a fresh episode search is queued.',
    );
  });

  $('#episode-reprobe-all')?.addEventListener('click', (event) => {
    if (!targetEpisode) return;
    episodeAction(
      event.currentTarget,
      `/api/series/${seriesId}/episodes/${targetEpisode}/candidates/probe-all`,
      { all: true, refresh: true },
      'Queueing ffprobe for every candidate of this episode.',
    );
  });

  $('#episode-remove-refind')?.addEventListener('click', (event) => {
    if (!targetEpisode) return;
    if (!window.confirm(
      'Delete any downloaded file for this episode and search again? The episode '
      + 'metadata is kept; the file and candidate scan are rebuilt.',
    )) return;
    episodeAction(
      event.currentTarget,
      `/api/series/${seriesId}/episodes/${targetEpisode}/remove-and-refind`,
      {},
      'File removed; a fresh episode search is queued.',
    );
  });

  // ------------------------------------------------------- manual metadata
  const metadataDialog = $('#series-metadata-dialog');
  const metadataQuery = $('#series-metadata-query');
  const metadataYear = $('#series-metadata-year');
  const metadataStatus = $('#series-metadata-status');
  const metadataResults = $('#series-metadata-results');
  let metadataToken = 0;

  function openManualMetadata() {
    if (!metadataDialog) return;
    open(metadataDialog);
    if (!metadataResults.childElementCount) searchMetadata();
  }

  $('#series-manual-metadata')?.addEventListener('click', openManualMetadata);
  $('#series-metadata-search')?.addEventListener('click', () => searchMetadata());
  metadataQuery?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      searchMetadata();
    }
  });
  metadataYear?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      searchMetadata();
    }
  });
  const debouncedMetadataSearch = debounce(() => searchMetadata(), 450);
  metadataQuery?.addEventListener('input', debouncedMetadataSearch);

  async function searchMetadata() {
    const query = metadataQuery.value.trim();
    if (!query) {
      setStatus(metadataStatus, 'Type a title to search TMDB.', 'bad');
      metadataQuery.focus();
      return;
    }
    const request = ++metadataToken;
    const year = metadataYear.value.trim();
    const params = new URLSearchParams({ q: query });
    if (year) params.set('year', year);
    metadataResults.replaceChildren(element('p', 'muted', 'Searching TMDB…'));
    setStatus(metadataStatus, '');
    try {
      const data = await get(
        `/api/import/metadata/series/${seriesId}/search?${params.toString()}`);
      if (request !== metadataToken) return;
      metadataResults.replaceChildren();
      if (!data.results.length) {
        metadataResults.append(element('p', 'muted',
          'No TMDB matches. Try a different title or clear the year.'));
        return;
      }
      data.results.forEach((result) => metadataResults.append(
        renderMetadataResult(result)));
    } catch (error) {
      if (request !== metadataToken) return;
      metadataResults.replaceChildren();
      setStatus(metadataStatus, error.message, 'bad');
    }
  }

  function renderMetadataResult(result) {
    const row = element('article', 'metadata-result');
    const posterBox = element('div', 'metadata-poster');
    if (result.poster) {
      const image = document.createElement('img');
      image.src = result.poster;
      image.alt = '';
      image.loading = 'lazy';
      posterBox.append(image);
    } else {
      posterBox.append(element('span', 'poster-fallback',
        (result.title || result.name || '?').slice(0, 1).toUpperCase()));
    }

    const body = element('div', 'metadata-result-body');
    const title = result.title || result.name || 'Untitled';
    body.append(element('strong', '',
      `${title}${result.year ? ` (${result.year})` : ''}`));
    const bits = [];
    if (result.original_title && result.original_title !== title) {
      bits.push(result.original_title);
    }
    if (result.kind) bits.push(result.kind === 'movie' ? 'Movie' : 'Series');
    if (result.release_date) bits.push(result.release_date);
    if (bits.length) body.append(element('span', 'muted small', bits.join(' · ')));
    if (result.overview) {
      body.append(element('p', 'metadata-overview', result.overview));
    }
    const isCurrent = result.in_library
      && Number(result.library_id) === Number(seriesId);
    if (result.in_library && !isCurrent) {
      body.append(element('span', 'queue-error small',
        `Already in the library as “${result.library_title}”.`));
    } else if (isCurrent) {
      body.append(element('span', 'hint',
        'This is the current TMDB identity; using it refreshes the episode metadata.'));
    }

    const use = element('button', 'btn btn-primary btn-sm',
      isCurrent ? 'Refresh' : 'Use this');
    use.type = 'button';
    use.disabled = Boolean(result.in_library && !isCurrent);
    if (use.disabled) {
      use.title = 'That TMDB series is already attached to another library entry.';
    }
    use.addEventListener('click', async () => {
      use.disabled = true;
      use.textContent = 'Applying…';
      try {
        const applied = await post(`/api/import/metadata/series/${seriesId}`, {
          tmdb_id: result.tmdb_id,
        });
        setStatus(metadataStatus,
          applied.warning || 'Metadata updated. Reloading…', 'ok');
        window.setTimeout(() => window.location.reload(), 700);
      } catch (error) {
        setStatus(metadataStatus, error.message, 'bad');
        use.disabled = false;
        use.textContent = isCurrent ? 'Refresh' : 'Use this';
      }
    });

    row.append(posterBox, body, use);
    return row;
  }

  // ------------------------------------------------------------ remove file
  const removeDialog = $('#episode-remove-dialog');
  const removeLabel = $('#episode-remove-label');
  const removeConfirm = $('#episode-remove-confirm');
  let removeFileKey = null;

  function openRemoveDialog(button) {
    removeFileKey = button.dataset.fileKey;
    removeLabel.textContent = button.dataset.episodeLabel || 'this episode';
    removeConfirm.disabled = false;
    removeConfirm.textContent = 'Remove file';
    open(removeDialog);
  }

  removeConfirm?.addEventListener('click', async () => {
    if (!removeFileKey) return;
    removeConfirm.disabled = true;
    removeConfirm.textContent = 'Removing…';
    setStatus(status, '');
    try {
      const result = await del('/api/storage/file', {
        path: removeFileKey,
        confirm: true,
        // The confirmation dialog explains that accepting here also cancels
        // any active download that still owns this path.
        confirm_active: true,
      });
      close(removeDialog);
      setStatus(status,
        result.warning || 'File removed; the episode is missing again.', 'ok');
      removeFileKey = null;
      window.setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      setStatus(status, error.message, 'bad');
      removeConfirm.disabled = false;
      removeConfirm.textContent = 'Remove file';
    }
  });

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
