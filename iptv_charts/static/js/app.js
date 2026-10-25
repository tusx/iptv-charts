/* IPTV Charts -- front-end behaviour.
 *
 * Plain JavaScript, no framework and no build step. The only non-trivial part
 * is sync polling: progress is fetched only while a sync is actually running,
 * so an idle app makes no requests at all. */

(() => {
  'use strict';

  const POLL_INTERVAL = 750;

  // Human wording for the sync engine's phase names.
  const PHASE_LABEL = {
    connecting: 'Connecting',
    account: 'Signing in',
    categories: 'Listing categories',
    download: 'Downloading',
    parse: 'Reading',
    indexing: 'Building search index',
  };

  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  let pollTimer = null;

  // ------------------------------------------------------------------ utils
  function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
  }

  async function postJSON(url, body, method = 'POST') {
    const options = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) options.body = JSON.stringify(body);
    const response = await fetch(url, options);
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      // Non-JSON error page; fall through to the status code.
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Request failed (HTTP ${response.status})`);
    }
    return payload;
  }

  // ----------------------------------------------------------------- dialog
  const dialog = $('#add-dialog');

  function openDialog() {
    if (!dialog) return;
    $('#probe-result').hidden = true;
    $('#probe-result').innerHTML = '';
    $('#add-submit-btn').disabled = false;
    dialog.showModal();
    $('#field-name').focus();
  }

  function closeDialog() {
    if (dialog?.open) dialog.close();
  }

  const addOpeners = ['#add-open-btn', '#add-open-btn-empty'];
  addOpeners.forEach((sel) => $(sel)?.addEventListener('click', openDialog));
  $('#add-close-btn')?.addEventListener('click', closeDialog);
  $('#add-cancel-btn')?.addEventListener('click', closeDialog);
  if (new URLSearchParams(window.location.search).get('add') === '1' && dialog) {
    openDialog();
    history.replaceState({}, '', window.location.pathname);
  }

  // Clicking the backdrop closes; clicking inside the form must not.
  dialog?.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });

  // Tabs ---------------------------------------------------------------------
  let currentKind = 'm3u';

  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      currentKind = tab.dataset.kind;
      $$('.tab').forEach((other) => other.classList.toggle('is-active', other === tab));
      $$('[data-panel]', dialog).forEach((panel) => {
        panel.hidden = panel.dataset.panel !== currentKind;
      });
    });
  });

  function readForm() {
    const value = (sel) => ($(sel)?.value || '').trim();
    return {
      kind: currentKind,
      name: value('#field-name'),
      m3u_url: value('#field-m3u'),
      user_agent: value('#field-ua'),
      referrer: value('#field-ref'),
      base_url: value('#field-base'),
      username: value('#field-user'),
      password: $('#field-pass')?.value || '',
      connection_limit: value('#field-limit'),
    };
  }

  // Renders the probe outcome. Written with textContent throughout -- none of
  // this is markup, and provider-supplied names end up in here.
  function renderProbe(container, ok, payload) {
    container.hidden = false;
    container.innerHTML = '';
    container.classList.toggle('is-ok', ok);
    container.classList.toggle('is-bad', !ok);

    const title = document.createElement('p');
    title.className = 'probe-title';
    title.textContent = ok ? '✓ Connection works' : '✕ Could not connect';
    container.appendChild(title);

    if (!ok) {
      const detail = document.createElement('p');
      detail.className = 'probe-line';
      detail.textContent = payload;
      container.appendChild(detail);
      return;
    }

    const rows = [];
    if (payload.kind === 'xtream') {
      const account = payload.account || {};
      const server = payload.server || {};
      if (account.status) rows.push(['Status', account.status]);
      if (account.exp_unknown) {
        rows.push(['Expires', 'no expiry set']);
      } else if (account.days_remaining !== null && account.days_remaining !== undefined) {
        rows.push(['Expires', `${account.days_remaining} day(s)`]);
      }
      if (account.max_connections) {
        rows.push([
          'Connections',
          `${account.active_connections || 0} / ${account.max_connections}`,
        ]);
      }
      if (account.allowed_formats?.length) {
        rows.push(['Formats', account.allowed_formats.join(', ')]);
      }
      if (server.host) rows.push(['Server', `${server.host}:${server.port || ''}`]);

      const counts = payload.category_counts || {};
      const parts = ['live', 'vod', 'series']
        .filter((kind) => counts[kind] !== null && counts[kind] !== undefined)
        .map((kind) => `${counts[kind]} ${kind}`);
      if (parts.length) rows.push(['Categories', parts.join(' · ')]);
    } else {
      if (payload.channels_estimate) {
        rows.push(['Channels', `about ${payload.channels_estimate.toLocaleString()}`]);
      } else if (payload.channels_sampled) {
        rows.push(['Channels so far', `${payload.channels_sampled.toLocaleString()} (sampled)`]);
      }
      if (payload.group_count_sampled) {
        rows.push(['Groups', `${payload.group_count_sampled} in sample`]);
      }
      if (payload.total_bytes) {
        rows.push(['Size', formatBytes(payload.total_bytes)]);
      }
      if (payload.epg_url) rows.push(['EPG', 'found']);
    }

    for (const [label, value] of rows) {
      const line = document.createElement('p');
      line.className = 'probe-line';
      const strong = document.createElement('span');
      strong.className = 'probe-key';
      strong.textContent = `${label}: `;
      line.appendChild(strong);
      line.appendChild(document.createTextNode(String(value)));
      container.appendChild(line);
    }

    (payload.warnings || []).forEach((warning) => {
      const line = document.createElement('p');
      line.className = 'probe-line probe-warn';
      line.textContent = `⚠ ${warning}`;
      container.appendChild(line);
    });
  }

  // Probe --------------------------------------------------------------------
  const probeBtn = $('#probe-btn');
  probeBtn?.addEventListener('click', async () => {
    const container = $('#probe-result');
    probeBtn.disabled = true;
    probeBtn.textContent = 'Testing…';
    try {
      const { result } = await postJSON('/api/probe', readForm());
      renderProbe(container, true, result);
    } catch (error) {
      renderProbe(container, false, error.message);
    } finally {
      probeBtn.disabled = false;
      probeBtn.textContent = 'Test connection';
    }
  });

  // Create -------------------------------------------------------------------
  $('#add-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submit = $('#add-submit-btn');
    submit.disabled = true;
    submit.textContent = 'Adding…';
    try {
      await postJSON('/api/sources', readForm());
      // A full page load is the simplest way to get the new card rendered
      // with its progress bar already in place.
      window.location.reload();
    } catch (error) {
      renderProbe($('#probe-result'), false, error.message);
      submit.disabled = false;
      submit.textContent = 'Add & sync';
    }
  });

  // ------------------------------------------------------------- sync actions
  async function syncOne(sourceId, button) {
    if (button) button.disabled = true;
    try {
      await postJSON(`/api/sources/${sourceId}/sync`);
      startPolling();
    } catch (error) {
      showCardError(sourceId, error.message);
      if (button) button.disabled = false;
    }
  }

  $$('.js-sync').forEach((button) => {
    button.addEventListener('click', () => syncOne(button.dataset.sourceId, button));
  });

  $('#sync-all-btn')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/sources/sync-all');
      startPolling();
    } catch (error) {
      window.alert(error.message);
    } finally {
      event.currentTarget.disabled = false;
    }
  });

  $('#cancel-all-btn')?.addEventListener('click', async () => {
    try {
      await postJSON('/api/sync/cancel-all');
    } catch { /* nothing useful to do */ }
  });

  $$('.js-cancel').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await postJSON(`/api/sources/${button.dataset.sourceId}/cancel`);
      } catch { /* already finished, most likely */ }
    });
  });

  $$('.source-limit-form').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const input = form.querySelector('input[type="number"]');
      button.disabled = true;
      try {
        await postJSON(`/api/sources/${form.dataset.sourceId}`, {
          connection_limit: input.value === '' ? null : Number(input.value),
        }, 'PATCH');
        window.location.reload();
      } catch (error) {
        showCardError(form.dataset.sourceId, error.message);
        button.disabled = false;
      }
    });
  });

  $$('.js-delete').forEach((button) => {
    button.addEventListener('click', async () => {
      const name = button.dataset.sourceName || 'this playlist';
      if (!window.confirm(`Remove ${name}? The downloaded catalog for it will be deleted.`)) {
        return;
      }
      try {
        await postJSON(`/api/sources/${button.dataset.sourceId}`, undefined, 'DELETE');
        window.location.reload();
      } catch (error) {
        showCardError(button.dataset.sourceId, error.message);
      }
    });
  });

  // -------------------------------------------------------------- progress UI
  function showCardError(sourceId, message) {
    const card = document.querySelector(`.source-card[data-source-id="${sourceId}"]`);
    const slot = $('.js-error', card || document);
    if (!slot) return;
    slot.hidden = false;
    slot.textContent = message;
  }

  function applyJob(card, job) {
    const slot = $('.sync-slot', card);
    const phase = $('.js-sync-phase', card);
    const count = $('.js-sync-count', card);
    const bar = $('.js-sync-bar', card);
    const progress = $('.progress', card);
    const syncBtn = $('.js-sync', card);
    const cancelBtn = $('.js-cancel', card);
    const errorNote = $('.js-error', card);

    if (!slot) return;

    const active = job && (job.state === 'queued' || job.state === 'running');

    if (!job || (!active && job.state === 'done' && !job.error)) {
      slot.hidden = true;
    } else if (active) {
      slot.hidden = false;
    } else {
      // Finished with an error or a cancellation -- keep it visible briefly
      // so the outcome is not just a silently vanishing bar.
      slot.hidden = false;
    }

    if (syncBtn) syncBtn.disabled = !!active;
    if (cancelBtn) cancelBtn.hidden = !active;

    if (!job) return;

    if (job.state === 'queued') {
      if (phase) phase.textContent = 'Queued';
      if (count) count.textContent = '';
      progress?.classList.add('progress-indeterminate');
      return;
    }

    if (job.state === 'running') {
      if (phase) {
        // Name the phase explicitly. Progress fills twice -- once downloading,
        // once parsing the spooled file -- and without the label the second
        // fill looks like the bar is broken rather than like the next step.
        const activity = PHASE_LABEL[job.phase] || '';
        phase.textContent = job.detail
          ? (activity ? `${activity} — ${job.detail}` : job.detail)
          : (activity || 'Working');
      }
      if (count) {
        const bits = [];
        if (job.items_done) bits.push(`${job.items_done.toLocaleString()} items`);
        if (job.bytes_total) {
          bits.push(`${formatBytes(job.bytes_done)} / ${formatBytes(job.bytes_total)}`);
        } else if (job.bytes_done) {
          bits.push(formatBytes(job.bytes_done));
        }
        count.textContent = bits.join(' · ');
      }
      if (job.fraction === null || job.fraction === undefined) {
        progress?.classList.add('progress-indeterminate');
      } else {
        progress?.classList.remove('progress-indeterminate');
        if (bar) bar.style.width = `${(job.fraction * 100).toFixed(1)}%`;
      }
      return;
    }

    // Terminal states.
    progress?.classList.remove('progress-indeterminate');
    if (job.state === 'done') {
      if (phase) phase.textContent = `Updated — ${job.message || 'done'}`;
      if (count) count.textContent = '';
      if (bar) bar.style.width = '100%';
    } else if (job.state === 'cancelled') {
      if (phase) phase.textContent = job.message || 'Cancelled';
      if (count) count.textContent = '';
      if (bar) bar.style.width = '0%';
    } else if (job.state === 'error') {
      if (phase) phase.textContent = 'Update failed';
      if (count) count.textContent = '';
      if (bar) bar.style.width = '0%';
      if (errorNote) {
        errorNote.hidden = false;
        errorNote.textContent = job.error || 'Unknown error';
      }
    }
  }

  function applySnapshot(snapshot) {
    // Per-card progress.
    $$('.source-card[data-source-id]').forEach((card) => {
      const sourceId = Number(card.dataset.sourceId);
      const job = snapshot.jobs[sourceId];
      applyJob(card, job);
      const badge = card.querySelector('.home-sync-badge');
      if (badge) {
        const active = job && (job.state === 'queued' || job.state === 'running');
        badge.hidden = !active;
      }
      const state = card.querySelector('.home-sync-state');
      if (state && job) {
        if (job.state === 'queued' || job.state === 'running') {
          state.textContent = job.message || 'Syncing…';
        } else if (job.state === 'error') {
          state.textContent = 'Last update failed';
        } else if (job.state === 'done') {
          state.textContent = 'Updated just now';
        }
      }
    });

    // Batch header.
    const banner = $('#batch-progress');
    if (banner) {
      const showBatch = snapshot.active && snapshot.batch_total > 1;
      banner.hidden = !showBatch;
      if (showBatch) {
        const bar = $('#batch-bar');
        const label = $('#batch-label');
        if (bar) {
          bar.style.width =
            snapshot.overall === null ? '35%' : `${(snapshot.overall * 100).toFixed(1)}%`;
        }
        if (label) {
          // The queue no longer has a durable batch counter; do not invent
          // "0 of N" while the worker is serialising the syncs.
          label.textContent =
            `Updating ${snapshot.batch_total} playlist${snapshot.batch_total === 1 ? '' : 's'}`;
        }
        if (snapshot.overall === null) {
          banner.querySelector('.progress')?.classList.add('progress-indeterminate');
        } else {
          banner.querySelector('.progress')?.classList.remove('progress-indeterminate');
        }
      }
    }

    const status = $('#global-sync-status');
    if (status) {
      const active = Object.values(snapshot.jobs).filter(
        (job) => job.state === 'running' || job.state === 'queued',
      );
      status.textContent = active.length
        ? `${active.length} syncing…`
        : '';
    }
  }

  // ------------------------------------------------------------------ polling
  async function poll() {
    try {
      const snapshot = await (await fetch('/api/sync/status', { cache: 'no-store' })).json();
      applySnapshot(snapshot);
      if (snapshot.active) {
        schedulePoll();
      } else {
        stopPolling();
      }
    } catch {
      // Transient failure (usually the server restarting under --dev). Try
      // again rather than giving up on progress reporting permanently.
      schedulePoll();
    }
  }

  function schedulePoll() {
    // setTimeout chaining rather than setInterval: a slow response would let
    // setInterval stack overlapping requests.
    clearTimeout(pollTimer);
    if (document.hidden) return;   // resume on visibilitychange
    pollTimer = setTimeout(poll, POLL_INTERVAL);
  }

  function startPolling() {
    clearTimeout(pollTimer);
    poll();
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && pollTimer === null) startPolling();
  });

  // On load, fetch once. If a sync is running (started in another tab, or
  // before a reload) this picks it up and begins polling.
  startPolling();
})();
