/* Movie detail page: actions, editing, manual search and deletion. */

(() => {
  'use strict';

  const {
    $, $$, get, post, patch, del, setStatus, debounce, element, open, close, feedback,
    mediaFacts,
  } = Manager;

  const movieId = window.MOVIE_ID;
  if (!movieId) return;

  const status = $('#movie-action-status');
  const releasesDialog = $('#movie-search-dialog');
  const releaseStatus = $('#movie-release-status');
  const releaseList = $('#movie-release-list');
  const releaseQuery = $('#movie-release-query');
  const allCandidatesButton = $('#movie-all-candidates');
  const candidateStatus = $('#candidate-action-status');
  const candidateFilterStatus = $('#candidate-filter-status');
  let releaseToken = 0;
  let allCandidatesMode = false;

  // ------------------------------------------------------- stage / history
  // Both panels are server-rendered, so the tabs only reveal markup that is
  // already on the page: switching never leaves an empty card while fetching.
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
  if (window.location.hash === '#movie-stage-history') {
    activateStageTab('history');
  }

  $('#movie-stage-refresh')?.addEventListener('click', () => window.location.reload());

  // A04 may expose /api/movies/<id>/stage. When it is present the server
  // renders MOVIE_STAGE_URL; poll it and reload only when the durable stage,
  // status, score, progress or newest event actually changed. Without the
  // endpoint the server-rendered card plus the action reloads are the refresh
  // path, and no request is made.
  if (window.MOVIE_STAGE_URL) {
    let signature = window.MOVIE_STAGE_SIGNATURE || '';
    const stageSignature = (data) => {
      if (!data || typeof data !== 'object') return '';
      const context = typeof data.stage === 'string'
        ? data
        : (data.stage_context || data.context || data.data || {});
      const events = context.events || [];
      const progress = context.progress || {};
      const work = context.work || {};
      return [
        context.stage || work.stage || '',
        context.status || work.status || '',
        context.message || '',
        context.best_score ?? work.best_score ?? '',
        progress.text || '',
        events.length ? (events[0].id || '') : '',
      ].join('|');
    };
    window.setInterval(async () => {
      if (document.hidden) return;
      try {
        const data = await get(window.MOVIE_STAGE_URL);
        const next = stageSignature(data);
        if (next && next !== signature) {
          signature = next;
          window.location.reload();
        }
      } catch (error) {
        // Keep the server render; a missing endpoint or a transient read
        // error must never make the detail page spin or go blank.
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

  $('#movie-search-now')?.addEventListener('click', (event) => {
    action(event.currentTarget, () => post(`/api/movies/${movieId}/search`),
      'Search finished.', { reload: true });
  });

  $('#movie-refresh')?.addEventListener('click', (event) => {
    action(event.currentTarget, () => post(`/api/movies/${movieId}/refresh`),
      'Metadata refreshed.', { reload: true });
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

  document.querySelectorAll('[data-cancel-download]').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        await post(`/api/downloads/${button.dataset.cancelDownload}/cancel`);
        feedback(button, 'Cancelling');
        window.setTimeout(() => window.location.reload(), 600);
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  });

  // ---------------------------------------------------------- candidates
  function setAllCandidatesMode(enabled) {
    allCandidatesMode = enabled;
    if (allCandidatesButton) {
      allCandidatesButton.textContent = enabled
        ? 'Show release matches'
        : 'Show all candidates';
      allCandidatesButton.classList.toggle('is-active', enabled);
    }
  }

  function openManualSearch() {
    releaseQuery.value = '';
    setAllCandidatesMode(false);
    open(releasesDialog);
    loadReleases('');
  }

  $('#movie-manual-search')?.addEventListener('click', openManualSearch);
  $('#movie-manual-search-section')?.addEventListener('click', openManualSearch);
  allCandidatesButton?.addEventListener('click', () => {
    setAllCandidatesMode(!allCandidatesMode);
    loadReleases(releaseQuery.value.trim());
  });
  releaseQuery.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      loadReleases(releaseQuery.value.trim());
    }
  });

  $('#candidate-probe-all')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Probing…';
    setStatus(candidateStatus, 'Queueing ffprobe for the candidates…');
    try {
      const result = await post(`/api/movies/${movieId}/candidates/probe-all`, {});
      setStatus(candidateStatus,
        `Probed ${result.probed || 0} candidate(s); ${result.failed || 0} failed.`, 'ok');
      window.setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      setStatus(candidateStatus, error.message, 'bad');
      button.disabled = false;
      button.textContent = original;
    }
  });

  document.addEventListener('click', async (event) => {
    const probe = event.target.closest('[data-probe-source]');
    if (probe) {
      probe.disabled = true;
      const original = probe.textContent;
      probe.textContent = 'Probing…';
      try {
        await post(`/api/movies/${movieId}/candidates/probe`, {
          source_id: Number(probe.dataset.probeSource),
          remote_id: probe.dataset.probeRemote,
          refresh: probe.dataset.probeRefresh === '1',
        });
        setStatus(candidateStatus, 'Probe finished.', 'ok');
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        setStatus(candidateStatus, error.message, 'bad');
        probe.disabled = false;
        probe.textContent = original;
      }
      return;
    }

    const pin = event.target.closest('[data-pin-source]');
    if (pin) {
      const mode = pin.dataset.pinMode;
      if (mode === 'only' && !window.confirm(
        'Use only this candidate? Other active downloads for this movie will be cancelled and the quality profile will be bypassed.'
      )) return;
      pin.disabled = true;
      const original = pin.textContent;
      pin.textContent = 'Saving…';
      try {
        const result = await post(`/api/movies/${movieId}/candidate`, {
          source_id: Number(pin.dataset.pinSource),
          remote_id: pin.dataset.pinRemote,
          mode,
          grab: true,
        });
        setStatus(candidateStatus,
          mode === 'only'
            ? 'This version is pinned and queued; other candidates were overridden.'
            : 'Search queued; this version is added unless the profile picks the same candidate.',
          'ok');
        window.setTimeout(() => window.location.reload(), 700);
      } catch (error) {
        setStatus(candidateStatus, error.message, 'bad');
        pin.disabled = false;
        pin.textContent = original;
      }
      return;
    }

    const clearPin = event.target.closest('#candidate-clear');
    if (clearPin) {
      clearPin.disabled = true;
      try {
        await del(`/api/movies/${movieId}/candidate`);
        setStatus(candidateStatus, 'Candidate pin cleared.', 'ok');
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        setStatus(candidateStatus, error.message, 'bad');
        clearPin.disabled = false;
      }
      return;
    }

    const removeAlternate = event.target.closest('[data-delete-alternate]');
    if (removeAlternate) {
      if (!window.confirm('Remove this additional version and delete its file from disk?')) return;
      removeAlternate.disabled = true;
      try {
        await del(`/api/movies/${movieId}/alternates/${removeAlternate.dataset.deleteAlternate}`);
        window.location.reload();
      } catch (error) {
        setStatus(candidateStatus, error.message, 'bad');
        removeAlternate.disabled = false;
      }
    }
  });

  $('#candidate-filters')?.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-candidate-filter]');
    if (!chip) return;
    const filter = chip.dataset.candidateFilter;
    $('#candidate-filters').querySelectorAll('[data-candidate-filter]').forEach((entry) => {
      const active = entry === chip;
      entry.classList.toggle('is-active', active);
      entry.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    let shown = 0;
    document.querySelectorAll('[data-candidate]').forEach((card) => {
      const visible =
        filter === 'all'
        || (filter === 'probed' && card.dataset.probe === 'ok')
        || (filter === 'allowed' && card.dataset.allowed === 'yes')
        || (filter === 'selected' && card.dataset.selected === 'yes');
      card.hidden = !visible;
      if (visible) shown += 1;
    });
    setStatus(candidateFilterStatus,
      `${shown} candidate${shown === 1 ? '' : 's'} shown.`);
  });

  // ------------------------------------------------------------ manual search
  const debouncedLoad = debounce(() => loadReleases(releaseQuery.value.trim()), 300);
  releaseQuery.addEventListener('input', debouncedLoad);

  async function loadReleases(query) {
    const request = ++releaseToken;
    const manualMode = allCandidatesMode;
    releaseList.replaceChildren(element(
      'p', 'muted',
      manualMode
        ? 'Loading every catalog candidate…'
        : 'Searching your playlists and probing the best releases…',
    ));
    setStatus(releaseStatus, manualMode
      ? 'All candidates are shown, including ones outside the quality profile.'
      : '');
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
          ? `/api/movies/${movieId}/candidates${suffix}`
          : `/api/movies/${movieId}/releases${suffix}`,
      );
      if (request !== releaseToken) return;
      const rows = manualMode ? (data.candidates || []) : (data.releases || []);
      releaseList.replaceChildren();
      if (!rows.length) {
        releaseList.append(element(
          'p', 'muted',
          manualMode
            ? 'No catalog candidates matched. Try a shorter or different search.'
            : 'No matching releases found. Try a different search or “Show all candidates”.',
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
    main.append(element('strong', '', release.name));
    const bits = [
      release.source_name || 'Unknown playlist',
      release.candidate_year || release.year || 'year ?',
      release.resolution || 'unknown',
      release.source && release.source !== 'unknown' ? release.source.toUpperCase() : null,
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
          await post(`/api/movies/${movieId}/candidates/probe`, {
            source_id: release.source_id,
            remote_id: release.remote_id,
            refresh: release.probe_status !== 'not_probed',
          });
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
      if (manualMode && !window.confirm(
        'Download this candidate as the main file? This overrides the quality profile '
        + 'and cancels other active downloads for this movie. ffprobe runs first if the '
        + 'candidate is not probed yet.',
      )) return;
      grab.disabled = true;
      grab.textContent = 'Queuing…';
      try {
        if (manualMode) {
          await post(`/api/movies/${movieId}/candidate`, {
            source_id: release.source_id,
            remote_id: release.remote_id,
            mode: 'only',
            grab: true,
            probe_if_needed: true,
          });
        } else {
          await post(`/api/movies/${movieId}/grab`, {
            source_id: release.source_id,
            remote_id: release.remote_id,
          });
        }
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

  // ------------------------------------------------------- manual metadata
  const metadataDialog = $('#movie-metadata-dialog');
  const metadataQuery = $('#movie-metadata-query');
  const metadataYear = $('#movie-metadata-year');
  const metadataStatus = $('#movie-metadata-status');
  const metadataResults = $('#movie-metadata-results');
  let metadataToken = 0;

  function openManualMetadata() {
    if (!metadataDialog) return;
    open(metadataDialog);
    if (!metadataResults.childElementCount) searchMetadata();
  }

  $('#movie-manual-metadata')?.addEventListener('click', openManualMetadata);
  $('#movie-metadata-search')?.addEventListener('click', () => searchMetadata());
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
        `/api/import/metadata/movies/${movieId}/search?${params.toString()}`);
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
    body.append(element('strong', '', `${title}${result.year ? ` (${result.year})` : ''}`));
    const bits = [];
    if (result.original_title && result.original_title !== title) {
      bits.push(result.original_title);
    }
    if (result.kind) bits.push(result.kind === 'movie' ? 'Movie' : 'Series');
    if (bits.length) body.append(element('span', 'muted small', bits.join(' · ')));
    if (result.overview) {
      body.append(element('p', 'metadata-overview', result.overview));
    }
    const isCurrent = result.in_library
      && Number(result.library_id) === Number(movieId);
    if (result.in_library && !isCurrent) {
      body.append(element('span', 'queue-error small',
        `Already in the library as “${result.library_title}”.`));
    } else if (isCurrent) {
      body.append(element('span', 'hint',
        'This is the current TMDB identity; using it refreshes the metadata.'));
    }

    const use = element('button', 'btn btn-primary btn-sm', isCurrent ? 'Refresh' : 'Use this');
    use.type = 'button';
    use.disabled = Boolean(result.in_library && !isCurrent);
    if (use.disabled) {
      use.title = 'That TMDB item is already attached to another library entry.';
    }
    use.addEventListener('click', async () => {
      use.disabled = true;
      use.textContent = 'Applying…';
      try {
        const applied = await post(`/api/import/metadata/movies/${movieId}`, {
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
