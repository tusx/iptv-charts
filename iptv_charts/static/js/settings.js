/* Settings page: player, metadata keys, naming, downloads, automation and
 * backup. Each card is independent and saves only its own fields. */

(() => {
  'use strict';

  const $ = (selector) => document.querySelector(selector);

  function setStatus(element, message, kind = '') {
    if (!element) return;
    element.textContent = message || '';
    element.classList.toggle('is-bad', kind === 'bad');
    element.classList.toggle('is-ok', kind === 'ok');
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || `Request failed (HTTP ${response.status})`);
    }
    return result;
  }

  async function withButton(button, working, fn) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = working;
    try {
      return await fn();
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  // ------------------------------------------------------------------ player
  const playerForm = $('#settings-form');
  if (playerForm) {
    const status = $('#settings-status');
    const save = $('#settings-save');
    const previewCard = $('#settings-preview-card');
    const preview = $('#settings-preview');

    function showCommand(argv) {
      if (!Array.isArray(argv) || !argv.length) {
        previewCard.hidden = true;
        return;
      }
      preview.textContent = argv.join('\n');
      previewCard.hidden = false;
    }

    playerForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const chosen = playerForm.querySelector('input[name="preferred_player"]:checked');
      const payload = {
        preferred_player: chosen ? chosen.value : 'auto',
        player_command: $('#player-command').value.trim(),
      };
      setStatus(status, '');
      try {
        const result = await withButton(save, 'Saving…', () => postJSON('/api/settings', payload));
        setStatus(status, result.player ? `Saved — playing via ${result.player.label}` : 'Saved', 'ok');
        showCommand(result.command);
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });

    playerForm.addEventListener('change', () => {
      const chosen = playerForm.querySelector('input[name="preferred_player"]:checked');
      const command = $('#player-command').value.trim();
      if (chosen && chosen.value === 'custom' && !command) {
        setStatus(status, 'Give a command below, then save.', 'bad');
      } else if (status.classList.contains('is-bad')) {
        setStatus(status, '');
      }
    });
  }

  // ---------------------------------------------------------------- metadata
  const metadataForm = $('#metadata-form');
  if (metadataForm) {
    const status = $('#metadata-status');
    metadataForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const payload = { language: $('#metadata-language').value.trim() };
      const tmdbClear = $('#tmdb-clear');
      const tvdbClear = $('#tvdb-clear');
      const tmdbValue = $('#tmdb-key').value.trim();
      const tvdbValue = $('#tvdb-key').value.trim();
      const pinValue = $('#tvdb-pin').value.trim();
      if (tmdbClear.checked) payload.tmdb_api_key = '';
      else if (tmdbValue) payload.tmdb_api_key = tmdbValue;
      if (tvdbClear.checked) {
        payload.tvdb_api_key = '';
        payload.tvdb_pin = '';
      } else {
        if (tvdbValue) payload.tvdb_api_key = tvdbValue;
        if (pinValue) payload.tvdb_pin = pinValue;
      }
      setStatus(status, '');
      try {
        const result = await withButton($('#metadata-save'), 'Saving…',
          () => postJSON('/api/settings/metadata', payload));
        setStatus(status,
          `Saved. TMDB ${result.tmdb ? 'configured' : 'not configured'}, ` +
          `TVDB ${result.tvdb ? 'configured' : 'not configured'}.`, 'ok');
        $('#tmdb-key').value = '';
        $('#tvdb-key').value = '';
        $('#tvdb-pin').value = '';
        tmdbClear.checked = false;
        tvdbClear.checked = false;
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });

    document.querySelectorAll('[data-test-metadata]').forEach((button) => {
      button.addEventListener('click', async () => {
        setStatus(status, 'Testing…');
        try {
          const result = await withButton(button, 'Testing…',
            () => postJSON('/api/metadata/test', { service: button.dataset.testMetadata }));
          setStatus(status, result.message || 'Connection succeeded.', 'ok');
        } catch (error) {
          setStatus(status, error.message, 'bad');
        }
      });
    });
  }

  // ------------------------------------------------------------------- media
  const mediaForm = $('#media-form');
  if (mediaForm) {
    const status = $('#media-status');
    const preview = $('#media-preview');
    mediaForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const payload = {
        movie_folder_template: $('#movie-folder-template').value.trim(),
        movie_file_template: $('#movie-file-template').value.trim(),
        series_folder_template: $('#series-folder-template').value.trim(),
        season_folder_template: $('#season-folder-template').value.trim(),
        episode_file_template: $('#episode-file-template').value.trim(),
      };
      setStatus(status, '');
      try {
        const result = await withButton($('#media-save'), 'Saving…',
          () => postJSON('/api/settings/media', payload));
        setStatus(status, 'Saved.', 'ok');
        preview.textContent = [
          `Movie folder: ${result.preview.movie_folder}`,
          `Movie file:   ${result.preview.movie_file}.mkv`,
          `Episode file: ${result.preview.episode_file}.mkv`,
        ].join('\n');
        preview.hidden = false;
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  }

  // --------------------------------------------------------------- downloads
  const downloadsForm = $('#downloads-form');
  if (downloadsForm) {
    const status = $('#downloads-status');
    downloadsForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      setStatus(status, '');
      try {
        await withButton($('#downloads-save'), 'Saving…', () => postJSON('/api/settings/downloads', {
          concurrency: Number($('#download-concurrency').value),
          max_attempts: Number($('#download-attempts').value),
          probe_cache_hours: Number($('#probe-cache-hours').value),
          probe_timeout_seconds: Number($('#probe-timeout').value),
          probe_max_candidates: Number($('#probe-max-candidates').value),
          probe_search_budget: Number($('#probe-search-budget').value),
        }));
        setStatus(status, 'Saved.', 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  }

  const clearProbes = $('#probes-clear');
  if (clearProbes) {
    clearProbes.addEventListener('click', async () => {
      const status = $('#downloads-status');
      setStatus(status, 'Clearing…');
      try {
        const result = await withButton(clearProbes, 'Clearing…',
          () => postJSON('/api/probes/clear', {}));
        setStatus(status, `Cleared ${result.cleared} cached probe(s).`, 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  }

  // --------------------------------------------------------------- scheduler
  const schedulerForm = $('#scheduler-form');
  if (schedulerForm) {
    const status = $('#scheduler-status');
    schedulerForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      setStatus(status, '');
      try {
        await withButton($('#scheduler-save'), 'Saving…',
          () => postJSON('/api/settings/scheduler', {
            search_interval_hours: Number($('#search-interval').value),
            refresh_interval_hours: Number($('#refresh-interval').value),
            catalog_refresh_hours: Number($('#catalog-interval').value),
          }));
        setStatus(status, 'Saved.', 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  }

  // ------------------------------------------------------------------ backup
  const exportButton = $('#backup-export');
  if (exportButton) {
    const status = $('#backup-status');
    exportButton.addEventListener('click', async () => {
      exportButton.disabled = true;
      setStatus(status, 'Building the backup…');
      try {
        const response = await fetch('/api/backup/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.error || `Request failed (HTTP ${response.status})`);
        }
        const blob = await response.blob();
        const disposition = response.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="?([^";]+)"?/i);
        const filename = match ? match[1] : 'iptv-charts-backup.zip';
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = filename;
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        setStatus(status, 'Backup downloaded. Keep it private — it contains secrets.', 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      } finally {
        exportButton.disabled = false;
      }
    });
  }

  const restoreButton = $('#backup-restore');
  if (restoreButton) {
    const status = $('#backup-status');
    restoreButton.addEventListener('click', async () => {
      const file = $('#backup-file').files[0];
      if (!file) {
        setStatus(status, 'Choose a backup ZIP first.', 'bad');
        return;
      }
      if (!window.confirm(
        'Restore this backup? The current library, settings and playlists will be replaced. ' +
        'A playlist sync will be queued afterwards.')) {
        return;
      }
      restoreButton.disabled = true;
      setStatus(status, 'Restoring…');
      try {
        const response = await fetch('/api/backup/restore', {
          method: 'POST',
          headers: { 'Content-Type': 'application/zip' },
          body: file,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.ok === false) {
          throw new Error(result.error || `Request failed (HTTP ${response.status})`);
        }
        const counts = result.counts || {};
        setStatus(status,
          `Restored ${counts.sources || 0} playlist(s) and ${counts.movies || 0} movie(s). ` +
          'Reloading…', 'ok');
        window.setTimeout(() => { window.location.href = '/'; }, 1200);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        restoreButton.disabled = false;
      }
    });
  }
})();
