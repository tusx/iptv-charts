/* Playlist edit page: save provider details, test unsaved details, remove. */

(() => {
  'use strict';

  const { $, post, patch, del, setStatus, element } = Manager;

  const sourceId = window.SOURCE_ID;
  const kind = window.SOURCE_KIND;
  if (!sourceId) return;

  const form = $('#source-edit-form');
  const status = $('#source-edit-status');
  const testResult = $('#source-test-result');
  const saveButton = $('#source-save');
  const testButton = $('#source-test');

  function payload() {
    const data = {
      name: $('#source-name').value.trim(),
      connection_limit: $('#source-connection-limit').value === ''
        ? null : Number($('#source-connection-limit').value),
      user_agent: $('#source-user-agent').value.trim(),
      referrer: $('#source-referrer').value.trim(),
    };
    if (kind === 'xtream') {
      data.base_url = $('#source-base-url').value.trim();
      data.username = $('#source-username').value.trim();
      const password = $('#source-password').value;
      if (password) data.password = password;
    } else {
      data.m3u_url = $('#source-m3u-url').value.trim();
    }
    return data;
  }

  function validationMessage(data) {
    if (!data.name) return 'Give the playlist a name.';
    if (kind === 'xtream') {
      if (!data.base_url) return 'Enter the server URL.';
      if (!data.username) return 'Enter the username.';
    } else if (!data.m3u_url) {
      return 'Enter the playlist URL.';
    }
    if (data.connection_limit !== null
        && (!Number.isInteger(data.connection_limit) || data.connection_limit < 1)) {
      return 'The connection limit must be a whole number of at least 1.';
    }
    return '';
  }

  async function withButton(button, label, fn) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label;
    try {
      return await fn();
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function renderTest(result) {
    testResult.replaceChildren();
    testResult.hidden = false;
    testResult.classList.remove('is-ok', 'is-bad');
    if (!result) {
      testResult.classList.add('is-bad');
      testResult.append(element('p', 'probe-title', '✕ Connection failed'));
      return;
    }
    testResult.classList.add('is-ok');
    testResult.append(element('p', 'probe-title', '✓ Connection works'));

    const lines = [];
    if (result.kind === 'xtream') {
      const account = result.account || {};
      const server = result.server || {};
      if (account.status) lines.push(`Account: ${account.status}`);
      if (account.max_connections) lines.push(`Provider connections: ${account.max_connections}`);
      if (server.host) lines.push(`Server: ${server.host}${server.port ? `:${server.port}` : ''}`);
      const counts = result.category_counts || {};
      const bits = Object.entries(counts)
        .filter(([, value]) => value !== null && value !== undefined)
        .map(([name, value]) => `${name} ${value}`);
      if (bits.length) lines.push(`Categories: ${bits.join(', ')}`);
    } else {
      if (result.channels_estimate) lines.push(`Channels (estimated): ${result.channels_estimate}`);
      else if (result.channels_sampled) lines.push(`Channels sampled: ${result.channels_sampled}`);
      if (result.group_count_sampled) lines.push(`Groups sampled: ${result.group_count_sampled}`);
      if (result.sample_covers_whole_file) lines.push('The sample covers the whole playlist.');
    }
    if ((result.warnings || []).length) lines.push(`Warnings: ${result.warnings.join(' ')}`);
    lines.forEach((line) => testResult.append(element('p', 'probe-line', line)));
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = payload();
    const problem = validationMessage(data);
    if (problem) {
      setStatus(status, problem, 'bad');
      return;
    }
    setStatus(status, 'Saving…');
    try {
      const result = await withButton(saveButton, 'Saving…',
        () => patch(`/api/sources/${sourceId}`, data));
      const bits = ['Saved'];
      if (result.catalog_changed) {
        bits.push(result.sync_queued
          ? 'catalog sync queued'
          : 'catalog sync could not be queued; update the playlist manually');
      }
      setStatus(status, `${bits.join(' — ')}.`, 'ok');
      window.setTimeout(() => { window.location.href = '/playlists'; }, 900);
    } catch (error) {
      setStatus(status, error.message, 'bad');
    }
  });

  testButton.addEventListener('click', async () => {
    const data = payload();
    const problem = validationMessage(data);
    if (problem) {
      setStatus(status, problem, 'bad');
      return;
    }
    testResult.hidden = true;
    setStatus(status, 'Testing the provider…');
    try {
      const result = await withButton(testButton, 'Testing…',
        () => post(`/api/sources/${sourceId}/probe`, data));
      setStatus(status, 'Connection test finished.', 'ok');
      renderTest(result.result);
    } catch (error) {
      setStatus(status, error.message, 'bad');
      renderTest(null);
    }
  });

  $('#source-remove')?.addEventListener('click', async () => {
    if (!window.confirm(`Remove ${window.SOURCE_NAME}? Its catalog will be deleted from the app.`)) {
      return;
    }
    try {
      await del(`/api/sources/${sourceId}`);
      window.location.href = '/playlists';
    } catch (error) {
      setStatus(status, error.message, 'bad');
    }
  });
})();
