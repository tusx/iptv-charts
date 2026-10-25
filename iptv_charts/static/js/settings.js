/* Global settings page: metadata providers and the refresh schedule save
 * atomically with one button. Player, naming, downloads/probing and section
 * search defaults moved to the IPTV/Movies/Series settings pages.
 *
 * Provider tests and backup/restore are actions, not settings, and stay
 * outside the form. */

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
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = working;
    try {
      return await fn();
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  const form = $('#settings-form');
  const status = $('#settings-status');
  const saveButton = $('#settings-save');

  function collect() {
    const metadata = {
      language: $('#metadata-language').value.trim(),
      tmdb_clear: $('#tmdb-clear').checked,
      tvdb_clear: $('#tvdb-clear').checked,
    };
    const tmdb = $('#tmdb-token').value.trim();
    const tvdb = $('#tvdb-key').value.trim();
    const pin = $('#tvdb-pin').value.trim();
    if (tmdb) metadata.tmdb_read_token = tmdb;
    if (tvdb) metadata.tvdb_api_key = tvdb;
    if (pin) metadata.tvdb_pin = pin;

    return {
      metadata,
      scheduler: {
        refresh_interval_hours: Number($('#refresh-interval').value),
        catalog_refresh_hours: Number($('#catalog-interval').value),
      },
    };
  }

  function focusSection(section) {
    const ids = {
      metadata: '#metadata-settings',
      scheduler: '#scheduler-settings',
    };
    const target = $(ids[section] || '');
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    target.querySelector('input, select, textarea')?.focus({ preventScroll: true });
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    setStatus(status, 'Saving global settings…');
    try {
      const result = await withButton(saveButton, 'Saving…',
        () => postJSON('/api/settings/save', collect()));
      const bits = ['Saved'];
      const meta = result.metadata || {};
      bits.push(`TMDB ${meta.tmdb ? 'configured' : 'not configured'}`);
      bits.push(`TVDB ${meta.tvdb ? 'configured' : 'not configured'}`);
      if ((result.warnings || []).length) bits.push(result.warnings.join(' '));
      setStatus(status, `${bits.join(' — ')}.`, 'ok');
      // Never keep a submitted secret in the DOM after a successful save.
      $('#tmdb-token').value = '';
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
