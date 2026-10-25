/* Movies/Series section settings pages.
 *
 * One main form per page with one Save button. Naming inputs also drive a
 * debounced preview endpoint; the server validates the templates, so the
 * preview cannot save and does not use a second, drifting renderer.
 * The shared task-policy editor PATCHes bworker policies and resets only
 * failed attempts.
 */

(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function setStatus(element, message, kind = '') {
    if (!element) return;
    element.textContent = message || '';
    element.classList.toggle('is-ok', kind === 'ok');
    element.classList.toggle('is-bad', kind === 'bad');
  }

  async function sendJSON(url, method, payload) {
    const response = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    let result = {};
    try {
      result = await response.json();
    } catch {
      // A non-JSON error page: report the status only.
    }
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

  function focusSection(root, section) {
    const kind = root.dataset.settingsSection;
    const prefix = kind === 'series' ? 'series' : 'movie';
    const ids = {
      naming: `#${prefix}-naming-settings`,
      defaults: `#${prefix}-defaults-settings`,
      search: `#${prefix}-search-settings`,
    };
    const target = $(ids[section] || '');
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    target.querySelector('input, select')?.focus({ preventScroll: true });
  }

  function groupFor(key) {
    if (key.endsWith('_template')) return 'naming';
    if (key.startsWith('default_')) return 'defaults';
    return 'search';
  }

  function collectSettings(form) {
    const payload = { naming: {}, defaults: {}, search: {} };
    $$('[data-setting]', form).forEach((input) => {
      const key = input.dataset.setting;
      let value = input.type === 'checkbox' ? input.checked : input.value.trim();
      if (input.type === 'number') value = Number(value);
      payload[groupFor(key)][key] = value;
    });
    return payload;
  }

  function renderPreview(kind, preview) {
    const element = $(kind === 'series' ? '#series-naming-preview' : '#movie-naming-preview');
    if (!element || !preview) return;
    if (kind === 'series') {
      element.textContent = [
        `Series folder: ${preview.series_folder}`,
        `Season folder: ${preview.season_folder}`,
        `Episode file:  ${preview.episode_file}.mkv`,
      ].join('\n');
    } else {
      element.textContent = [
        `Movie folder: ${preview.movie_folder}`,
        `Movie file:   ${preview.movie_file}.mkv`,
      ].join('\n');
    }
  }

  function initSectionPage() {
    const form = $('[data-settings-section]');
    if (!form) return;
    const kind = form.dataset.settingsSection;
    const status = $(`#${kind}-settings-status`);
    const namingStatus = $(`#${kind}-naming-status`);
    const saveButton = $(`#${kind}-settings-save`);
    const saveUrl = form.dataset.saveUrl;
    const previewUrl = form.dataset.previewUrl;
    let previewTimer = null;
    let previewToken = 0;

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const payload = collectSettings(form);
      setStatus(status, 'Saving…');
      try {
        const result = await withButton(saveButton, 'Saving…',
          () => sendJSON(saveUrl, 'POST', payload));
        setStatus(status, 'Saved.', 'ok');
        renderPreview(kind, result.preview);
        setStatus(namingStatus, '');
      } catch (error) {
        setStatus(status, error.message, 'bad');
        focusSection(form, error.section);
      }
    });

    const namingInputs = $$('[data-setting$="_template"]', form);
    function schedulePreview() {
      if (!previewUrl) return;
      window.clearTimeout(previewTimer);
      previewTimer = window.setTimeout(async () => {
        const token = ++previewToken;
        const templates = {};
        namingInputs.forEach((input) => { templates[input.dataset.setting] = input.value.trim(); });
        try {
          const result = await sendJSON(previewUrl, 'POST', { templates });
          if (token !== previewToken) return;
          renderPreview(kind, result.preview);
          setStatus(namingStatus, '');
        } catch (error) {
          if (token !== previewToken) return;
          setStatus(namingStatus, error.message, 'bad');
        }
      }, 250);
    }
    namingInputs.forEach((input) => input.addEventListener('input', schedulePreview));
  }

  /* ------------------------------------------------------------ policies */
  function policyRowValues(row) {
    const value = (name) => row.querySelector(`[data-policy-field="${name}"]`);
    const enabled = value('enabled');
    const infinite = value('infinite_retry');
    const maxFailures = value('max_failures');
    const delay = value('retry_delay_seconds');
    const backoff = value('backoff_multiplier');
    const maxDelay = value('max_delay_seconds');
    const changes = {
      enabled: Boolean(enabled && enabled.checked),
      infinite_retry: Boolean(infinite && infinite.checked),
      retry_delay_seconds: Number(delay.value),
      backoff_multiplier: Number(backoff.value),
      max_delay_seconds: maxDelay.value === '' ? null : Number(maxDelay.value),
    };
    if (!changes.infinite_retry) {
      changes.max_failures = maxFailures.value === '' ? null : Number(maxFailures.value);
    }
    return changes;
  }

  function applyPolicyRow(row, policy) {
    const value = (name) => row.querySelector(`[data-policy-field="${name}"]`);
    const enabled = value('enabled');
    const infinite = value('infinite_retry');
    const maxFailures = value('max_failures');
    if (enabled) enabled.checked = Boolean(policy.enabled);
    if (infinite) infinite.checked = Boolean(policy.infinite_retry);
    if (maxFailures) {
      maxFailures.value = policy.max_failures ?? '';
      maxFailures.disabled = Boolean(policy.infinite_retry);
    }
    const delay = value('retry_delay_seconds');
    const backoff = value('backoff_multiplier');
    const maxDelay = value('max_delay_seconds');
    if (delay) delay.value = policy.retry_delay_seconds ?? '';
    if (backoff) backoff.value = policy.backoff_multiplier ?? '';
    if (maxDelay) maxDelay.value = policy.max_delay_seconds ?? '';
  }

  function initPolicyEditor() {
    const form = $('[data-policy-editor]');
    if (!form) return;
    const status = $('#policy-status');
    const saveButton = $('#policy-save');
    const resetButton = $('#policy-reset');
    const rows = $$('[data-work-type]', form);

    rows.forEach((row) => {
      const infinite = row.querySelector('[data-policy-field="infinite_retry"]');
      const maxFailures = row.querySelector('[data-policy-field="max_failures"]');
      if (!infinite || !maxFailures || row.dataset.locked === '1') return;
      infinite.addEventListener('change', () => {
        maxFailures.disabled = infinite.checked;
        if (infinite.checked) maxFailures.value = '';
      });
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const policies = [];
      let incomplete = null;
      rows.forEach((row) => {
        if (row.dataset.locked === '1') return;
        const infinite = row.querySelector('[data-policy-field="infinite_retry"]');
        const maxFailures = row.querySelector('[data-policy-field="max_failures"]');
        if (infinite && !infinite.checked && (!maxFailures || maxFailures.value === '')) {
          incomplete = maxFailures;
          return;
        }
        policies.push({ work_type: row.dataset.workType, changes: policyRowValues(row) });
      });
      if (incomplete) {
        incomplete.focus();
        setStatus(status, 'Give a maximum number of failures, or choose Retry forever.', 'bad');
        return;
      }
      setStatus(status, 'Saving task policies…');
      try {
        const result = await withButton(saveButton, 'Saving…',
          () => sendJSON(form.dataset.policiesUrl, 'PATCH', { policies }));
        const byType = Object.fromEntries((result.policies || []).map((row) => [row.work_type, row]));
        rows.forEach((row) => {
          const policy = byType[row.dataset.workType];
          if (policy) applyPolicyRow(row, policy);
        });
        setStatus(status, 'Task policies saved.', 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });

    resetButton?.addEventListener('click', async () => {
      if (!window.confirm(
        'Reset every failed task? Failed units return to pending and run again on the next loop. ' +
        'Completed and pending work is not changed.')) {
        return;
      }
      setStatus(status, 'Resetting failed attempts…');
      try {
        const result = await withButton(resetButton, 'Resetting…',
          () => sendJSON(form.dataset.resetUrl, 'POST', {}));
        setStatus(status, result.message || 'Failed attempts reset.', 'ok');
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    });
  }

  initSectionPage();
  initPolicyEditor();
})();
