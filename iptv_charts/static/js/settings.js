/* Settings page: one form, one atomic save, plus action buttons that are not
 * settings (provider tests, probe-cache clear, backup/restore). */

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
      const error = new Error(result.error || `Request failed (HTTP ${response.status})`);
      error.section = result.section || '';
      throw error;
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

  const form = $('#settings-form');
  const status = $('#settings-status');
  const saveButton = $('#settings-save');
  const previewCard = $('#settings-preview-card');
  const preview = $('#settings-preview');
  const mediaPreview = $('#media-preview');

  function showCommand(argv) {
    if (!Array.isArray(argv) || !argv.length) {
      if (previewCard) previewCard.hidden = true;
      return;
    }
    if (!preview) return;
    preview.textContent = argv.join('\n');
    if (previewCard) previewCard.hidden = false;
  }

  function showMediaPreview(results) {
    if (!mediaPreview || !results) return;
    mediaPreview.textContent = [
      `Movie folder: ${results.movie_folder}`,
      `Movie file:   ${results.movie_file}.mkv`,
      `Episode file: ${results.episode_file}.mkv`,
    ].join('\n');
    mediaPreview.hidden = false;
  }

  function selectedPlayer() {
    const chosen = form.querySelector('input[name="preferred_player"]:checked');
    return chosen ? chosen.value : 'auto';
  }

  function collect() {
    const metadata = {
      language: $('#metadata-language').value.trim(),
      tmdb_clear: $('#tmdb-clear').checked,
      tvdb_clear: $('#tvdb-clear').checked,
    };
    const tmdb = $('#tmdb-key').value.trim();
    const tvdb = $('#tvdb-key').value.trim();
    const pin = $('#tvdb-pin').value.trim();
    if (tmdb) metadata.tmdb_api_key = tmdb;
    if (tvdb) metadata.tvdb_api_key = tvdb;
    if (pin) metadata.tvdb_pin = pin;

    return {
      player: {
        preferred_player: selectedPlayer(),
        player_command: $('#player-command').value.trim(),
      },
      metadata,
      media: {
        movie_folder_template: $('#movie-folder-template').value.trim(),
        movie_file_template: $('#movie-file-template').value.trim(),
        series_folder_template: $('#series-folder-template').value.trim(),
        season_folder_template: $('#season-folder-template').value.trim(),
        episode_file_template: $('#episode-file-template').value.trim(),
      },
      downloads: {
        concurrency: Number($('#download-concurrency').value),
        max_attempts: Number($('#download-attempts').value),
        probe_cache_hours: Number($('#probe-cache-hours').value),
        probe_timeout_seconds: Number($('#probe-timeout').value),
        probe_max_candidates: Number($('#probe-max-candidates').value),
        probe_search_budget: Number($('#probe-search-budget').value),
      },
      scheduler: {
        search_interval_hours: Number($('#search-interval').value),
        refresh_interval_hours: Number($('#refresh-interval').value),
        catalog_refresh_hours: Number($('#catalog-interval').value),
      },
    };
  }

  function focusSection(section) {
    const ids = {
      player: '#player-settings',
      metadata: '#metadata-settings',
      media: '#media-settings',
      downloads: '#download-settings',
      scheduler: '#scheduler-settings',
    };
    const id = ids[section] || '';
    const target = id ? document.querySelector(id) : null;
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    target.querySelector('input, select, textarea')?.focus({ preventScroll: true });
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = collect();
    if (payload.player.preferred_player === 'custom' && !payload.player.player_command) {
      setStatus(status, 'Give the custom command first, or choose another player.', 'bad');
      focusSection('player');
      return;
    }
    setStatus(status, 'Saving all settings…');
    try {
      const result = await withButton(saveButton, 'Saving…',
        () => postJSON('/api/settings/save', payload));
      const bits = ['Saved'];
      if (result.player) bits.push(`playing via ${result.player.label}`);
      if ((result.warnings || []).length) bits.push(result.warnings.join(' '));
      setStatus(status, `${bits.join(' — ')}.`, 'ok');
      showCommand(result.command);
      showMediaPreview(result.media?.preview);
      $('#tmdb-key').value = '';
      $('#tvdb-key').value = '';
      $('#tvdb-pin').value = '';
      $('#tmdb-clear').checked = false;
      $('#tvdb-clear').checked = false;
    } catch (error) {
      setStatus(status, error.message, 'bad');
      focusSection(error.section);
    }
  });

  // ---------------------------------------------------------- action buttons
  document.querySelectorAll('[data-test-metadata]').forEach((button) => {
    button.addEventListener('click', async () => {
      setStatus(status, 'Testing the saved provider key…');
      try {
        const result = await withButton(button, 'Testing…',
          () => postJSON('/api/metadata/test', {
            service: button.dataset.testMetadata,
          }));
        setStatus(status, result.message || 'Connection succeeded.', 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  });

  const clearProbes = $('#probes-clear');
  if (clearProbes) {
    clearProbes.addEventListener('click', async () => {
      setStatus(status, 'Clearing the probe cache…');
      try {
        const result = await withButton(clearProbes, 'Clearing…',
          () => postJSON('/api/probes/clear', {}));
        setStatus(status, `Cleared ${result.cleared} cached probe(s).`, 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  }

  // ------------------------------------------------------ backup and restore
  const exportButton = $('#backup-export');
  if (exportButton) {
    const backupStatus = $('#backup-status');
    exportButton.addEventListener('click', async () => {
      exportButton.disabled = true;
      setStatus(backupStatus, 'Building the backup…');
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
        setStatus(backupStatus, 'Backup downloaded. Keep it private — it contains secrets.', 'ok');
      } catch (error) {
        setStatus(backupStatus, error.message, 'bad');
      } finally {
        exportButton.disabled = false;
      }
    });
  }

  const restoreButton = $('#backup-restore');
  if (restoreButton) {
    const backupStatus = $('#backup-status');
    restoreButton.addEventListener('click', async () => {
      const file = $('#backup-file').files[0];
      if (!file) {
        setStatus(backupStatus, 'Choose a backup ZIP first.', 'bad');
        return;
      }
      if (!window.confirm(
        'Restore this backup? The current library, settings and playlists will be replaced. ' +
        'A playlist sync will be queued afterwards.')) {
        return;
      }
      restoreButton.disabled = true;
      setStatus(backupStatus, 'Restoring…');
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
        setStatus(backupStatus,
          `Restored ${counts.sources || 0} playlist(s) and ${counts.movies || 0} movie(s). ` +
          'Reloading…', 'ok');
        window.setTimeout(() => { window.location.href = '/'; }, 1200);
      } catch (error) {
        setStatus(backupStatus, error.message, 'bad');
        restoreButton.disabled = false;
      }
    });
  }
})();
