/* Activity page: queue, recent failures, history and scheduled tasks. */

(() => {
  'use strict';

  const {
    $, get, post, patch, del, setStatus, element, feedback, formatBytes, timeAgo,
  } = Manager;

  const queueBody = $('#queue-body');
  const historyBody = $('#history-body');
  const tasksBody = $('#tasks-body');
  const queueCount = $('#queue-count');
  const status = $('#activity-status');
  let timer = null;

  const taskLabels = {
    search_missing: 'Search missing',
    refresh_metadata: 'Refresh metadata',
    refresh_catalog: 'Refresh playlists',
    scan_library: 'Scan library',
  };

  function schedule(delay) {
    window.clearTimeout(timer);
    if (delay !== null) timer = window.setTimeout(refresh, delay);
  }

  async function refresh() {
    try {
      const data = await get('/api/activity');
      renderQueue(data.active, data.recent || []);
      renderHistory(data.history || []);
      renderTasks(data.tasks || [], data.task_running);
      const busy = (data.active || []).length > 0 || data.task_running;
      schedule(busy ? 1500 : 5000);
    } catch (error) {
      setStatus(status, error.message, 'bad');
      schedule(5000);
    }
  }

  function badge(label, kind) {
    return element('span', `badge badge-${kind}`, label);
  }

  function renderQueue(active, recent) {
    queueCount.textContent = active.length
      ? `${active.length} active`
      : 'Idle';
    queueBody.replaceChildren();
    if (!active.length && !recent.length) {
      queueBody.append(element('p', 'muted', 'Nothing is downloading right now.'));
      return;
    }

    if (active.length) {
      const list = element('div', 'queue-list');
      active.forEach((download) => list.append(renderQueueRow(download, true)));
      queueBody.append(list);
    }

    if (recent.length) {
      queueBody.append(element('h3', 'subheading', 'Recent failures and cancellations'));
      const list = element('div', 'queue-list');
      recent.forEach((download) => list.append(renderQueueRow(download, false)));
      queueBody.append(list);
    }
  }

  function renderQueueRow(download, active) {
    const row = element('div', 'queue-row');
    const main = element('div', 'queue-main');
    main.append(element('strong', '', download.title));
    const bits = [
      download.kind === 'episode' ? 'Episode' : 'Movie',
      download.source_name || 'Unknown playlist',
      download.quality_label || download.resolution || 'unknown',
      `priority ${download.priority || 0}`,
    ];
    main.append(element('span', 'muted', bits.join(' · ')));
    if (download.release_name) main.append(element('span', 'muted small', download.release_name));
    if (download.error) main.append(element('span', 'queue-error', download.error));

    const state = element('div', 'queue-state');
    state.append(badge(download.status, download.status));
    if (active) {
      const fraction = download.fraction;
      const progress = element('div', 'progress');
      const bar = element('span', `progress-bar${fraction === null || fraction === undefined ? ' progress-indeterminate' : ''}`);
      if (fraction !== null && fraction !== undefined) {
        bar.style.width = `${Math.max(0, Math.min(100, fraction * 100)).toFixed(1)}%`;
      }
      progress.append(bar);
      state.append(progress);
      state.append(element('span', 'muted small',
        `${formatBytes(download.bytes_done)} / ${download.bytes_total ? formatBytes(download.bytes_total) : 'unknown'}`));
    }

    const actions = element('div', 'queue-actions');
    const priority = document.createElement('input');
    priority.type = 'number';
    priority.className = 'priority-input';
    priority.min = '-100';
    priority.max = '100';
    priority.step = '1';
    priority.value = String(download.priority || 0);
    priority.title = 'Queue priority — higher runs first';
    priority.dataset.priority = download.id;
    actions.append(priority);
    if (active) {
      const cancel = element('button', 'btn btn-sm btn-danger', 'Cancel');
      cancel.type = 'button';
      cancel.dataset.cancel = download.id;
      actions.append(cancel);
    } else {
      const retry = element('button', 'btn btn-sm', 'Retry');
      retry.type = 'button';
      retry.dataset.retry = download.id;
      actions.append(retry);
      const remove = element('button', 'btn btn-sm btn-ghost', 'Remove');
      remove.type = 'button';
      remove.dataset.remove = download.id;
      actions.append(remove);
    }

    row.append(main, state, actions);
    return row;
  }

  function renderHistory(history) {
    historyBody.replaceChildren();
    if (!history.length) {
      historyBody.append(element('p', 'muted', 'No history yet.'));
      return;
    }
    const table = element('div', 'history-table');
    history.forEach((event) => {
      const row = element('div', 'history-row');
      row.append(badge(event.event, event.event));
      const main = element('div', 'history-main');
      main.append(element('strong', '', event.title));
      if (event.message) main.append(element('span', 'muted small', event.message));
      row.append(main, element('time', 'muted small', timeAgo(event.created_at)));
      table.append(row);
    });
    historyBody.append(table);
  }

  function formatTimestamp(timestamp) {
    if (!timestamp) return '—';
    return new Date(Number(timestamp) * 1000).toLocaleString();
  }

  function renderTasks(tasks, running) {
    tasksBody.replaceChildren();
    if (!tasks.length) {
      tasksBody.append(element('p', 'muted', 'No task state recorded yet.'));
      return;
    }
    const table = element('div', 'task-list');
    tasks.forEach((task) => {
      const row = element('div', 'task-row');
      const main = element('div', 'task-main');
      main.append(element('strong', '', taskLabels[task.name] || task.name));
      main.append(element('span', 'muted small',
        `Last run: ${formatTimestamp(task.last_finished)} · Next: ${formatTimestamp(task.next_run)}`));
      if (task.message) main.append(element('span', 'task-message', task.message));
      const state = element('div', 'task-state');
      state.append(badge(task.status, task.status === 'error' ? 'failed' : task.status === 'running' ? 'downloading' : 'muted'));
      const run = element('button', 'btn btn-sm', 'Run now');
      run.type = 'button';
      run.dataset.runTask = task.name;
      run.disabled = Boolean(running);
      state.append(run);
      row.append(main, state);
      table.append(row);
    });
    tasksBody.append(table);
  }

  document.addEventListener('change', async (event) => {
    const priority = event.target.closest('[data-priority]');
    if (!priority) return;
    try {
      await patch(`/api/downloads/${priority.dataset.priority}`, {
        priority: Number(priority.value) || 0,
      });
      setStatus(status, 'Queue priority updated.', 'ok');
      refresh();
    } catch (error) {
      setStatus(status, error.message, 'bad');
    }
  });

  document.addEventListener('click', async (event) => {
    const run = event.target.closest('[data-run-task]');
    if (run) {
      run.disabled = true;
      try {
        await post(`/api/tasks/${run.dataset.runTask}/run`);
        setStatus(status, 'Task started.', 'ok');
        feedback(run, 'Started');
        window.setTimeout(refresh, 500);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        run.disabled = false;
      }
      return;
    }

    const cancel = event.target.closest('[data-cancel]');
    if (cancel) {
      try {
        await post(`/api/downloads/${cancel.dataset.cancel}/cancel`);
        setStatus(status, 'Cancelling the download.', 'ok');
        refresh();
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
      return;
    }

    const retry = event.target.closest('[data-retry]');
    if (retry) {
      try {
        await post(`/api/downloads/${retry.dataset.retry}/retry`);
        setStatus(status, 'Download queued again.', 'ok');
        refresh();
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
      return;
    }

    const remove = event.target.closest('[data-remove]');
    if (remove) {
      try {
        await del(`/api/downloads/${remove.dataset.remove}`);
        setStatus(status, 'Download entry removed.', 'ok');
        refresh();
      } catch (error) {
        setStatus(status, error.message, 'bad');
      }
    }
  });

  refresh();
})();
