/* Activity page: bworker card, live activity log, refresh control and the
 * queue/history/tasks summaries.
 *
 * The bworker API is the source of truth for pause/current work and the
 * in-memory log; /api/activity keeps the legacy download queue and scheduled
 * task state. Everything returned by those endpoints is untrusted data and is
 * inserted with textContent/DOM APIs, never as HTML strings.
 *
 * Auto-refresh is deliberately conservative: it stops while the page or the
 * log is scrolled away from the top, while the tab is hidden, or when the user
 * pauses it manually. The user's manual choice survives every automatic
 * pause/resume and every poll. */

(() => {
  'use strict';

  const {
    $, request, post, patch, del, setStatus, element, formatBytes, timeAgo,
  } = Manager;

  const card = $('#bworker-card');
  if (!card) return;

  const nodes = {
    statusLine: $('#activity-status'),
    state: $('#bworker-state'),
    heartbeat: $('#bworker-heartbeat'),
    pauseButton: $('#bworker-pause'),
    pauseLabel: $('#bworker-pause-label'),
    pauseIcon: $('#bworker-pause-icon'),
    resetButton: $('#bworker-reset'),
    currentLabel: $('#bworker-current-label'),
    currentStage: $('#bworker-current-stage'),
    currentStarted: $('#bworker-current-started'),
    currentStatus: $('#bworker-current-status'),
    pausedNote: $('#bworker-paused-note'),
    progress: $('#bworker-progress'),
    progressBar: $('#bworker-progress-bar'),
    progressFill: $('#bworker-progress-fill'),
    progressLabel: $('#bworker-progress-label'),
    currentEmpty: $('#bworker-current-empty'),
    log: $('#activity-log'),
    logList: $('#activity-log-list'),
    logEmpty: $('#activity-log-empty'),
    logCount: $('#activity-log-count'),
    degraded: $('#activity-degraded'),
    interval: $('#activity-refresh-interval'),
    refreshToggle: $('#activity-refresh-toggle'),
    refreshLabel: $('#activity-refresh-label'),
    refreshIcon: $('#activity-refresh-icon'),
    refreshPause: $('#activity-refresh-pause'),
    refreshPauseLabel: $('#activity-refresh-pause-label'),
    refreshPauseIcon: $('#activity-refresh-pause-icon'),
    refreshHint: $('#activity-refresh-hint'),
    queueCount: $('#queue-count'),
    queueBody: $('#queue-body'),
    historyBody: $('#history-body'),
    tasksBody: $('#tasks-body'),
    tasksState: $('#tasks-state'),
  };

  // Seconds. Kept in one list so the select and the server-side default can
  // never drift apart.
  const REFRESH_OPTIONS = [5, 10, 15, 20, 25, 30, 60,
                           120, 180, 300, 600, 900, 1200, 1800, 3600];

  const WORK_ACTIONS = {
    playlist_sync: (label) => `Updating playlist “${label}”`,
    movie_search: (label) => `Searching “${label}”`,
    series_search: (label) => `Searching “${label}”`,
    episode_search: (label) => `Searching “${label}”`,
    metadata_refresh: (label) => `Refreshing metadata for “${label}”`,
    download: (label) => `Downloading “${label}”`,
    probe: (label) => `Probing “${label}”`,
  };

  const ACTION_PREFIX = /^(updating|searching|downloading|probing|refreshing|scanning|cleaning)\b/i;

  const TASK_LABELS = {
    refresh_metadata: 'Refresh metadata',
    refresh_catalog: 'Refresh playlists',
    scan_library: 'Scan library',
    update_check: 'Check for updates',
  };

  const state = {
    interval: 5,
    remaining: 5,
    userPaused: false,
    hidden: Boolean(document.hidden),
    scrolledAway: window.scrollY > 0,
    logScrolled: false,
    inFlight: false,
    pendingPoll: false,
    generation: 0,
    wasActive: false,
    intervalTouched: false,
    serverDefaultApplied: false,
    bworkerPaused: false,
    entries: [],
    rows: new Map(),
    currentStartedAt: null,
  };

  // ---------------------------------------------------------------- utilities
  function toNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function pretty(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/_/g, ' ')
      .trim();
  }

  function durationText(value) {
    const total = Math.max(0, Math.floor(toNumber(value) || 0));
    if (total < 60) return `${total}s`;
    if (total < 3600) {
      const minutes = Math.floor(total / 60);
      const seconds = total % 60;
      return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
    }
    if (total < 86400) {
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
    }
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    return hours ? `${days}d ${hours}h` : `${days}d`;
  }

  function shortAgo(timestamp) {
    const seconds = toNumber(timestamp);
    if (seconds === null) return '—';
    const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
    if (elapsed < 1) return 'just now';
    return `${durationText(elapsed)} ago`;
  }

  function untilTime(timestamp) {
    const target = toNumber(timestamp);
    if (target === null) return '';
    const seconds = Math.floor(target - Date.now() / 1000);
    if (seconds <= 0) return 'due now';
    return `in ${durationText(seconds)}`;
  }

  function intervalText(seconds) {
    const value = Number(seconds) || 5;
    if (value < 60) return `${value}s`;
    return `${Math.round(value / 60)} min`;
  }

  function statusClass(status) {
    const value = String(status || '').toLowerCase();
    if (value === 'running') return 'pill-running';
    if (value === 'success' || value === 'done' || value === 'complete') return 'pill-ok';
    if (value === 'failure' || value === 'failed' || value === 'error') return 'pill-bad';
    if (value === 'retry' || value === 'paused' || value === 'interrupted'
        || value === 'warning') return 'pill-warn';
    return 'pill-muted';
  }

  function setPill(node, text, className) {
    if (!node) return;
    node.textContent = text;
    node.className = `pill ${className || 'pill-muted'}`;
  }

  function errorMessage(error) {
    const text = String(error && error.message ? error.message : (error || 'request failed'))
      .replace(/\s+/g, ' ')
      .trim();
    return text.slice(0, 240) || 'request failed';
  }

  function normaliseText(text) {
    return String(text || '')
      .toLowerCase()
      .replace(/[“”"]/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  // -------------------------------------------------------------- log entries
  function entryTitle(entry) {
    const workType = prettifyWorkType(entry && entry.work_type);
    const target = entry && entry.target && typeof entry.target === 'object'
      ? entry.target : {};
    const label = String(target.label || '');
    if (workType === 'library_scan') return 'Scanning the library';
    if (workType === 'activity_cleanup') return 'Cleaning the activity log';
    if (String(entry && entry.event || '') === 'paused') {
      return String((entry && entry.message) || 'Bworker paused');
    }
    if (String(entry && entry.event || '') === 'resumed') {
      return String((entry && entry.message) || 'Bworker resumed');
    }
    const action = WORK_ACTIONS[workType];
    if (action && label) {
      // Worker-job targets are already whole phrases ("Updating playlist
      // \"X\""); bworker work targets carry the bare entity name.
      if (ACTION_PREFIX.test(label)) return label;
      return action(label);
    }
    if (label) return label;
    if (entry && entry.message) return String(entry.message);
    return workType ? pretty(workType) : 'Activity';
  }

  function prettifyWorkType(value) {
    return String(value === null || value === undefined ? '' : value);
  }

  function entryDetail(entry, title) {
    const message = String(entry && entry.message ? entry.message : '').trim();
    if (!message) return '';
    if (normaliseText(message) === normaliseText(title)) return '';
    return message;
  }

  function entryMeta(entry) {
    const parts = [];
    const started = toNumber(entry && entry.started_at);
    const logged = toNumber(entry && entry.at);
    if (started !== null) parts.push(`started ${shortAgo(started)}`);
    else if (logged !== null) parts.push(`logged ${shortAgo(logged)}`);

    const status = String(entry && entry.status || '').toLowerCase();
    const finished = toNumber(entry && entry.finished_at);
    if (finished !== null) {
      parts.push(`finished ${shortAgo(finished)}`);
      const duration = toNumber(entry && entry.duration);
      if (duration !== null) parts.push(`took ${durationText(duration)}`);
    } else if (status === 'running' && started !== null) {
      parts.push(`running ${durationText(Math.max(0, Date.now() / 1000 - started))}`);
    } else if (status === 'paused') {
      parts.push('paused');
    }

    const next = toNumber(entry && entry.next_run_at);
    if (next !== null) parts.push(`next run ${untilTime(next)}`);
    const attempt = toNumber(entry && entry.attempt);
    if (attempt !== null && attempt > 1) parts.push(`attempt ${attempt}`);
    return parts.join(' · ');
  }

  function progressInfo(progress) {
    if (!progress || typeof progress !== 'object' || Array.isArray(progress)) {
      return null;
    }
    let fraction = toNumber(progress.fraction);
    let done = toNumber(progress.done);
    let total = toNumber(progress.total);
    if (done === null) done = toNumber(progress.items_done);
    if (total === null) total = toNumber(progress.items_total);
    const bytesDone = toNumber(progress.bytes_done);
    const bytesTotal = toNumber(progress.bytes_total);

    if (fraction === null && done !== null && total !== null && total > 0) {
      fraction = done / total;
    }
    if (fraction === null && bytesDone !== null && bytesTotal !== null
        && bytesTotal > 0) {
      fraction = bytesDone / bytesTotal;
    }

    const bits = [];
    if (fraction !== null) bits.push(`${Math.round(fraction * 100)}%`);
    if (bytesDone !== null && bytesTotal !== null && bytesTotal > 0) {
      bits.push(`${formatBytes(bytesDone)} / ${formatBytes(bytesTotal)}`);
    } else if (done !== null && total !== null) {
      bits.push(`${done} / ${total}`);
    } else if (done !== null) {
      bits.push(String(done));
    }
    if (progress.detail) bits.push(String(progress.detail));
    else if (progress.phase) bits.push(pretty(progress.phase));

    if (!bits.length) {
      const skip = new Set(['download_id', 'fraction', 'bytes_done',
                            'bytes_total', 'items_done', 'items_total',
                            'done', 'total', 'phase', 'detail']);
      for (const [key, value] of Object.entries(progress)) {
        if (skip.has(key)) continue;
        if (typeof value === 'string' || typeof value === 'number') {
          bits.push(`${pretty(key)} ${value}`);
        }
        if (bits.length >= 2) break;
      }
    }
    if (!bits.length && fraction === null) return null;
    return {
      fraction: fraction === null ? null : Math.max(0, Math.min(1, fraction)),
      text: bits.join(' · '),
    };
  }

  function buildLogRow() {
    const row = element('article', 'activity-log-entry');
    const head = element('div', 'activity-entry-head');
    const status = element('span', 'pill pill-muted');
    const eventTag = element('span', 'tag');
    const stageTag = element('span', 'tag');
    const meta = element('span', 'activity-entry-time');
    head.append(status, eventTag, stageTag, meta);

    const title = element('strong', 'activity-entry-title');
    const message = element('p', 'activity-entry-message');
    message.hidden = true;

    const progress = element('div', 'activity-entry-progress');
    progress.hidden = true;
    const progressBar = element('div', 'progress');
    const progressFill = element('span', 'progress-bar');
    progressBar.append(progressFill);
    const progressLabel = element('span', 'activity-entry-progress-label');
    progress.append(progressBar, progressLabel);

    row.append(head, title, message, progress);
    row.parts = { status, eventTag, stageTag, meta, title, message,
                  progress, progressBar, progressFill, progressLabel };
    return row;
  }

  function updateLogRow(row, entry) {
    const parts = row.parts;
    row.entry = entry;
    const status = String(entry.status || entry.event || 'unknown');
    parts.status.textContent = pretty(status) || 'unknown';
    parts.status.className = `pill ${statusClass(status)}`;
    parts.eventTag.textContent = `event: ${pretty(entry.event) || '—'}`;
    parts.stageTag.textContent = `stage: ${pretty(entry.stage) || '—'}`;
    parts.meta.textContent = entryMeta(entry);

    const title = entryTitle(entry);
    parts.title.textContent = title;
    const detail = entryDetail(entry, title);
    parts.message.textContent = detail;
    parts.message.hidden = !detail;

    const info = progressInfo(entry.progress);
    if (info) {
      parts.progress.hidden = false;
      parts.progressLabel.textContent = info.text || 'Working…';
      if (info.fraction !== null) {
        parts.progressBar.classList.remove('progress-indeterminate');
        parts.progressFill.style.width = `${(info.fraction * 100).toFixed(1)}%`;
      } else {
        parts.progressBar.classList.add('progress-indeterminate');
        parts.progressFill.style.width = '';
      }
    } else {
      parts.progress.hidden = true;
      parts.progressBar.classList.remove('progress-indeterminate');
      parts.progressFill.style.width = '0%';
      parts.progressLabel.textContent = '';
    }

    row.classList.toggle('activity-entry-paused', status === 'paused');
    row.dataset.entryId = entry.id !== null && entry.id !== undefined
      ? String(entry.id) : '';
  }

  function renderLog(payload) {
    const entries = (Array.isArray(payload.entries) ? payload.entries : [])
      .slice(0, 100);
    state.entries = entries;
    state.bworkerPaused = Boolean(payload.paused);
    renderPauseButton();

    const degraded = Boolean(payload.degraded) || payload.available === false;
    nodes.degraded.hidden = !degraded;

    // The bworker writes exactly one paused entry. While the pause flag is
    // visible but the writer has not appended it yet (it waits for the
    // current unit), show exactly one synthetic entry so the state is never
    // invisible. The synthetic row disappears when the real one arrives.
    const hasPausedEntry = entries.some((entry) => {
      if (String(entry.event || '') === 'paused') return true;
      return String(entry.status || '') === 'paused'
        && /paused/i.test(String(entry.message || ''));
    });
    const display = [];
    if (Boolean(payload.paused) && !hasPausedEntry) {
      display.push({
        id: null,
        synthetic: true,
        event: 'paused',
        status: 'paused',
        stage: 'paused',
        message: 'Bworker paused',
        started_at: null,
      });
    }
    display.push(...entries);

    const seen = new Set();
    display.forEach((entry) => {
      const key = entry.id !== null && entry.id !== undefined
        ? `id:${entry.id}` : 'synthetic:paused';
      seen.add(key);
      let row = state.rows.get(key);
      if (!row) {
        row = buildLogRow();
        state.rows.set(key, row);
      }
      updateLogRow(row, entry);
      nodes.logList.append(row);
    });
    for (const [key, row] of state.rows) {
      if (!seen.has(key)) {
        row.remove();
        state.rows.delete(key);
      }
    }

    const count = entries.length;
    nodes.logCount.textContent = count
      ? `${count} entr${count === 1 ? 'y' : 'ies'} shown`
      : (degraded ? 'log unavailable' : 'no entries');
    nodes.logEmpty.hidden = display.length > 0;
    if (!display.length) {
      nodes.logEmpty.textContent = degraded
        ? 'The live log is unavailable right now; current work is shown above.'
        : 'No activity yet. The bworker writes here when it starts work.';
    }
    nodes.log.setAttribute('aria-busy', 'false');

    if (!state.serverDefaultApplied && !state.intervalTouched) {
      state.serverDefaultApplied = true;
      const seconds = toNumber(payload.refresh_seconds);
      if (seconds !== null && REFRESH_OPTIONS.includes(seconds)) {
        state.interval = seconds;
        state.remaining = seconds;
        nodes.interval.value = String(seconds);
        updateRefreshUI();
      }
    }
    refreshTimes();
  }

  function refreshTimes() {
    for (const row of state.rows.values()) {
      if (row.entry) row.parts.meta.textContent = entryMeta(row.entry);
    }
    if (state.currentStartedAt !== null) {
      nodes.currentStarted.textContent =
        `started ${shortAgo(state.currentStartedAt)}`;
    }
  }

  // ---------------------------------------------------------------- bworker
  function renderPauseButton() {
    nodes.pauseLabel.textContent = state.bworkerPaused ? 'Resume' : 'Pause';
    nodes.pauseButton.setAttribute('aria-pressed', String(state.bworkerPaused));
    nodes.pauseButton.title = state.bworkerPaused
      ? 'Resume automatic bworker work'
      : 'Pause after the current unit finishes';
    nodes.pauseIcon.className =
      `fa-solid fa-fw fa-${state.bworkerPaused ? 'play' : 'pause'}`;
  }

  function setCount(key, value) {
    const node = $(`#bw-count-${key}`);
    if (!node) return;
    const number = toNumber(value);
    node.textContent = number === null ? '—' : String(Math.trunc(number));
  }

  function renderQueueCounts(queue) {
    if (!queue || typeof queue !== 'object') return;
    const work = queue.work && typeof queue.work === 'object' ? queue.work : {};
    const jobs = queue.worker_jobs && typeof queue.worker_jobs === 'object'
      ? queue.worker_jobs : {};
    setCount('work-pending', work.pending);
    setCount('work-running', work.running);
    setCount('work-retry', work.retry);
    setCount('work-stopped', work.failed_stopped);
    setCount('work-complete', work.complete);
    setCount('jobs-queued', jobs.queued);
    setCount('jobs-running', jobs.running);
  }

  function findRunningEntry(entries, current) {
    const running = entries.filter(
      (entry) => String(entry.status || '').toLowerCase() === 'running');
    if (!running.length) return null;
    if (current) {
      const workId = current.work_id;
      if (workId !== null && workId !== undefined) {
        const exact = running.find(
          (entry) => entry.work_id !== null && entry.work_id !== undefined
            && String(entry.work_id) === String(workId));
        if (exact) return exact;
      }
      const label = String(current.target && current.target.label
        || current.label || '');
      if (label) {
        const byLabel = running.find((entry) => String(
          entry.target && entry.target.label || '') === label);
        if (byLabel) return byLabel;
      }
    }
    return running[0];
  }

  function currentTitle(current) {
    const workType = prettifyWorkType(current.work_type);
    const target = current.target && typeof current.target === 'object'
      ? current.target : {};
    const label = String(target.label || current.label || '');
    if (workType === 'library_scan') return 'Scanning the library';
    if (workType === 'activity_cleanup') return 'Cleaning the activity log';
    const action = WORK_ACTIONS[workType];
    if (action && label) {
      if (ACTION_PREFIX.test(label)) return label;
      return action(label);
    }
    return current.label || label || 'Work in progress';
  }

  function renderCurrent(current, runningEntry, paused, alive) {
    const hasCurrent = Boolean(current && (current.label || current.target
      || current.work_type || current.stage));
    state.currentStartedAt = hasCurrent ? toNumber(current.started_at) : null;

    if (!hasCurrent) {
      nodes.currentLabel.textContent = 'No current work';
      nodes.currentStage.textContent = 'stage: —';
      nodes.currentStarted.textContent = 'started —';
      setPill(nodes.currentStatus, paused ? 'paused' : 'idle',
        paused ? 'pill-warn' : 'pill-muted');
      nodes.pausedNote.hidden = !paused;
      nodes.pausedNote.textContent = paused ? 'Paused — no work unit is running.' : '';
      nodes.currentEmpty.hidden = false;
      nodes.currentEmpty.textContent = !alive
        ? 'The bworker is not running.'
        : (paused ? 'Paused — no work unit is running.'
          : 'Idle — waiting for the next work unit.');
      nodes.progress.hidden = true;
      nodes.progressBar.classList.remove('progress-indeterminate');
      nodes.progressFill.style.width = '0%';
      nodes.progressLabel.textContent = '';
      return;
    }

    nodes.currentEmpty.hidden = true;
    nodes.currentLabel.textContent = currentTitle(current);
    const stage = pretty(current.stage);
    nodes.currentStage.textContent = `stage: ${stage || '—'}`;
    nodes.currentStarted.textContent = state.currentStartedAt !== null
      ? `started ${shortAgo(state.currentStartedAt)}` : 'started unknown';
    const status = String(current.status || (paused ? 'paused' : 'running'));
    setPill(nodes.currentStatus, pretty(status) || '—', statusClass(status));
    nodes.pausedNote.hidden = !paused;
    nodes.pausedNote.textContent = paused
      ? 'Pausing after the current unit finishes; no new work will start.' : '';

    const info = runningEntry ? progressInfo(runningEntry.progress) : null;
    const inProgress = status === 'running' || Boolean(runningEntry);
    if (info || inProgress) {
      nodes.progress.hidden = false;
      if (info && info.fraction !== null) {
        nodes.progressBar.classList.remove('progress-indeterminate');
        nodes.progressFill.style.width = `${(info.fraction * 100).toFixed(1)}%`;
        nodes.progressLabel.textContent = info.text
          || `${Math.round(info.fraction * 100)}%`;
      } else {
        nodes.progressBar.classList.add('progress-indeterminate');
        nodes.progressFill.style.width = '';
        nodes.progressLabel.textContent = (info && info.text) || 'Working…';
      }
    } else {
      nodes.progress.hidden = true;
      nodes.progressBar.classList.remove('progress-indeterminate');
      nodes.progressFill.style.width = '0%';
      nodes.progressLabel.textContent = '';
    }
  }

  function renderBworker(payload, logPayload) {
    const paused = Boolean(payload.paused);
    const alive = Boolean(payload.alive);
    const current = payload.current || (logPayload && logPayload.current) || null;
    const runningEntry = findRunningEntry(state.entries, current);

    state.bworkerPaused = paused;
    renderPauseButton();
    renderQueueCounts(payload.queue);

    let label = 'Idle';
    let kind = 'pill-muted';
    if (!alive) {
      label = paused ? 'Paused — not running' : 'Bworker not running';
      kind = 'pill-bad';
    } else if (paused) {
      label = 'Paused';
      kind = 'pill-warn';
    } else if (current) {
      label = 'Working';
      kind = 'pill-ok';
    }
    setPill(nodes.state, label, kind);

    const heartbeat = [];
    const age = toNumber(payload.heartbeat_age);
    if (age !== null) {
      heartbeat.push(age <= 1 ? 'heartbeat just now'
        : `heartbeat ${durationText(age)} ago`);
    } else if (payload.heartbeat_at) {
      heartbeat.push(`heartbeat ${shortAgo(payload.heartbeat_at)}`);
    } else {
      heartbeat.push('no heartbeat yet');
    }
    if (payload.status) heartbeat.push(`status: ${pretty(payload.status)}`);
    nodes.heartbeat.textContent = heartbeat.join(' · ');

    renderCurrent(current, runningEntry, paused, alive);
  }

  // Used only when /api/bworker/status failed but the activity endpoint still
  // answered: current work from SQLite is still worth showing.
  function renderBworkerFromActivity(payload) {
    const paused = Boolean(payload.paused);
    const current = payload.current || null;
    const runningEntry = findRunningEntry(state.entries, current);
    state.bworkerPaused = paused;
    renderPauseButton();
    setPill(nodes.state, paused ? 'Paused' : (current ? 'Working' : 'Idle'),
      paused ? 'pill-warn' : (current ? 'pill-ok' : 'pill-muted'));
    nodes.heartbeat.textContent = 'status unavailable — showing the last known state';
    renderCurrent(current, runningEntry, paused, true);
  }

  // ------------------------------------------------------- refresh scheduling
  function isRefreshActive() {
    return !state.userPaused && !state.hidden && !state.scrolledAway
      && !state.logScrolled;
  }

  function updateRefreshUI() {
    const active = isRefreshActive();
    const remaining = Math.max(1, Math.ceil(state.remaining));
    nodes.refreshLabel.textContent = active
      ? `Refresh on (${remaining}s)` : 'Refresh paused';
    nodes.refreshToggle.classList.toggle('is-on', active);
    nodes.refreshToggle.classList.toggle('is-off', !active);
    nodes.refreshToggle.setAttribute('aria-pressed', String(active));
    nodes.refreshIcon.className = active
      ? 'fa-solid fa-fw fa-rotate' : 'fa-solid fa-fw fa-pause';

    nodes.refreshPauseLabel.textContent = state.userPaused
      ? 'Resume refresh' : 'Pause refresh';
    nodes.refreshPauseIcon.className = state.userPaused
      ? 'fa-solid fa-fw fa-play' : 'fa-solid fa-fw fa-pause';
    nodes.refreshPause.setAttribute('aria-pressed', String(state.userPaused));

    let hint;
    if (state.userPaused) hint = 'Paused manually.';
    else if (state.hidden) hint = 'Paused while this tab is hidden.';
    else if (state.scrolledAway) hint = 'Paused while scrolled away from the top.';
    else if (state.logScrolled) hint = 'Paused while the activity log is scrolled.';
    else hint = `Refreshes every ${intervalText(state.interval)}.`;
    nodes.refreshHint.textContent = hint;
  }

  function refreshConditionsChanged() {
    const active = isRefreshActive();
    const becameActive = active && !state.wasActive;
    state.wasActive = active;
    if (becameActive) {
      state.remaining = state.interval;
      pollOnce();
    }
    updateRefreshUI();
  }

  function toggleManualPause() {
    state.userPaused = !state.userPaused;
    state.remaining = state.interval;
    refreshConditionsChanged();
  }

  function onScroll() {
    const away = window.scrollY > 0;
    if (away === state.scrolledAway) return;
    state.scrolledAway = away;
    state.remaining = state.interval;
    refreshConditionsChanged();
  }

  function onLogScroll() {
    const scrolled = nodes.log.scrollTop > 0;
    if (scrolled === state.logScrolled) return;
    state.logScrolled = scrolled;
    state.remaining = state.interval;
    refreshConditionsChanged();
  }

  function onVisibility() {
    state.hidden = Boolean(document.hidden);
    state.remaining = state.interval;
    refreshConditionsChanged();
  }

  function onIntervalChange() {
    const value = Number(nodes.interval.value);
    if (!REFRESH_OPTIONS.includes(value)) return;
    state.interval = value;
    state.remaining = value;
    state.intervalTouched = true;
    updateRefreshUI();
  }

  function tick() {
    refreshTimes();
    if (!isRefreshActive() || state.inFlight) {
      updateRefreshUI();
      return;
    }
    state.remaining = Math.max(0, state.remaining - 1);
    if (state.remaining <= 0) pollOnce();
    updateRefreshUI();
  }

  // ------------------------------------------------------------------ actions
  // An explicit action invalidates any snapshot that was already in flight.
  // Otherwise a status read started just before a pause could finish just
  // after it and put the button back into its old state.
  function invalidatePoll() {
    state.generation += 1;
  }

  async function toggleBworkerPause() {
    const endpoint = state.bworkerPaused
      ? '/api/bworker/resume' : '/api/bworker/pause';
    invalidatePoll();
    nodes.pauseButton.disabled = true;
    try {
      const payload = await post(endpoint);
      state.bworkerPaused = Boolean(payload.paused);
      renderPauseButton();
      // Invalidate again: a periodic read may have started while the write was
      // in flight and could still carry the pre-write pause flag.
      invalidatePoll();
      setStatus(nodes.statusLine, payload.message || (state.bworkerPaused
        ? 'Bworker paused.' : 'Bworker resumed.'), 'ok');
    } catch (error) {
      setStatus(nodes.statusLine, errorMessage(error), 'bad');
    } finally {
      nodes.pauseButton.disabled = false;
    }
    await pollOnce();
  }

  async function resetAll() {
    invalidatePoll();
    nodes.resetButton.disabled = true;
    try {
      const payload = await post('/api/bworker/reset', { all: true });
      invalidatePoll();
      const updated = Number(payload.updated || 0);
      const requested = Number(payload.reset_requested || 0);
      if (updated || requested) {
        const parts = [];
        if (updated) {
          parts.push(`Re-queued ${updated} work unit${updated === 1 ? '' : 's'}`);
        }
        if (requested) {
          parts.push(`${requested} will reset after the current unit`);
        }
        setStatus(nodes.statusLine, `${parts.join('; ')}.`, 'ok');
      } else {
        setStatus(nodes.statusLine, 'No failed attempts to reset.', 'ok');
      }
    } catch (error) {
      setStatus(nodes.statusLine, errorMessage(error), 'bad');
    } finally {
      nodes.resetButton.disabled = false;
    }
    await pollOnce();
  }

  // ------------------------------------------------------------- queue/history
  function badge(label, kind) {
    const token = String(kind || 'muted')
      .replace(/[^a-z0-9_-]/gi, '')
      .toLowerCase() || 'muted';
    return element('span', `badge badge-${token}`, label);
  }

  function formatTimestamp(timestamp) {
    if (!timestamp) return '—';
    return new Date(Number(timestamp) * 1000).toLocaleString();
  }

  function renderQueue(active, recent) {
    nodes.queueCount.textContent = active.length
      ? `${active.length} active` : 'Idle';
    nodes.queueBody.replaceChildren();
    if (!active.length && !recent.length) {
      nodes.queueBody.append(
        element('p', 'muted', 'Nothing is downloading right now.'));
      return;
    }

    if (active.length) {
      const list = element('div', 'queue-list');
      active.forEach((download) => list.append(renderQueueRow(download, true)));
      nodes.queueBody.append(list);
    }
    if (recent.length) {
      nodes.queueBody.append(
        element('h3', 'subheading', 'Recent failures and cancellations'));
      const list = element('div', 'queue-list');
      recent.forEach((download) => list.append(renderQueueRow(download, false)));
      nodes.queueBody.append(list);
    }
  }

  function renderQueueRow(download, active) {
    const row = element('div', 'queue-row');
    const main = element('div', 'queue-main');
    main.append(element('strong', '', download.title));

    const bits = [
      download.kind === 'episode' ? 'Episode' : 'Movie',
      download.mode === 'alternate' ? 'Additional version' : null,
      download.source_name || 'Unknown playlist',
      download.quality_label || download.resolution || 'unknown',
      `priority ${download.priority || 0}`,
    ].filter(Boolean);
    main.append(element('span', 'muted', bits.join(' · ')));
    if (download.release_name) {
      main.append(element('span', 'muted small', download.release_name));
    }
    if (download.error) {
      main.append(element('span', 'queue-error', download.error));
    }

    const stateBox = element('div', 'queue-state');
    stateBox.append(badge(download.status, download.status));
    if (active) {
      const fraction = download.fraction;
      const progress = element('div', 'progress');
      const bar = element('span',
        `progress-bar${fraction === null || fraction === undefined
          ? ' progress-indeterminate' : ''}`);
      if (fraction !== null && fraction !== undefined) {
        bar.style.width =
          `${Math.max(0, Math.min(100, fraction * 100)).toFixed(1)}%`;
      }
      progress.append(bar);
      stateBox.append(progress);
      stateBox.append(element('span', 'muted small',
        `${formatBytes(download.bytes_done)} / `
        + `${download.bytes_total ? formatBytes(download.bytes_total) : 'unknown'}`));
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

    row.append(main, stateBox, actions);
    return row;
  }

  function renderHistory(history) {
    nodes.historyBody.replaceChildren();
    if (!history.length) {
      nodes.historyBody.append(element('p', 'muted', 'No history yet.'));
      return;
    }
    const table = element('div', 'history-table');
    history.forEach((event) => {
      const row = element('div', 'history-row');
      row.append(badge(event.event, event.event));
      const main = element('div', 'history-main');
      main.append(element('strong', '', event.title));
      if (event.message) {
        main.append(element('span', 'muted small', event.message));
      }
      row.append(main, element('time', 'muted small',
        timeAgo(event.created_at)));
      table.append(row);
    });
    nodes.historyBody.append(table);
  }

  function renderTasks(tasks, running) {
    nodes.tasksBody.replaceChildren();
    nodes.tasksState.textContent = running ? 'A task is running.' : '';
    // The legacy manual search task is bworker work now; its row is not
    // shown at all.
    const visible = tasks.filter((task) => task.name !== 'search_missing');
    if (!visible.length) {
      nodes.tasksBody.append(
        element('p', 'muted', 'No task state recorded yet.'));
      return;
    }
    const table = element('div', 'task-list');
    visible.forEach((task) => {
      const row = element('div', 'task-row');
      const main = element('div', 'task-main');
      main.append(element('strong', '',
        TASK_LABELS[task.name] || task.name));
      main.append(element('span', 'muted small',
        `Last run: ${formatTimestamp(task.last_finished)}`
        + ` · Next: ${formatTimestamp(task.next_run)}`));
      if (task.message) {
        main.append(element('span', 'task-message', task.message));
      }
      const stateBox = element('div', 'task-state');
      stateBox.append(badge(task.status,
        task.status === 'error' ? 'failed'
          : (task.status === 'running' ? 'downloading' : 'muted')));
      row.append(main, stateBox);
      table.append(row);
    });
    nodes.tasksBody.append(table);
  }

  function renderDownloads(data) {
    renderQueue(Array.isArray(data.active) ? data.active : [],
      Array.isArray(data.recent) ? data.recent : []);
    renderHistory(Array.isArray(data.history) ? data.history : []);
    renderTasks(Array.isArray(data.tasks) ? data.tasks : [],
      Boolean(data.task_running));
  }

  // ------------------------------------------------------------------ polling
  async function pollOnce() {
    if (state.inFlight) {
      state.pendingPoll = true;
      return;
    }
    state.inFlight = true;
    state.remaining = state.interval;
    updateRefreshUI();

    const generation = state.generation;
    const errors = [];
    try {
      const results = await Promise.allSettled([
        request('/api/bworker/status', { cache: 'no-store' }),
        request('/api/bworker/activity?limit=100', { cache: 'no-store' }),
        request('/api/activity', { cache: 'no-store' }),
      ]);
      if (generation !== state.generation) {
        // An explicit action ran while these reads were in flight. Drop the
        // snapshot rather than letting it contradict the action's result.
        return;
      }
      const statusResult = results[0];
      const logResult = results[1];
      const downloadsResult = results[2];

      let logPayload = null;
      if (logResult.status === 'fulfilled') {
        logPayload = logResult.value && typeof logResult.value === 'object'
          ? logResult.value : {};
        renderLog(logPayload);
      } else {
        errors.push(errorMessage(logResult.reason));
        nodes.log.setAttribute('aria-busy', 'false');
        if (!state.rows.size) {
          nodes.logEmpty.hidden = false;
          nodes.logEmpty.textContent = 'The live log could not be loaded.';
        }
      }

      if (statusResult.status === 'fulfilled') {
        const payload = statusResult.value && typeof statusResult.value === 'object'
          ? statusResult.value : {};
        renderBworker(payload, logPayload);
      } else if (logPayload) {
        errors.push(errorMessage(statusResult.reason));
        renderBworkerFromActivity(logPayload);
      } else {
        errors.push(errorMessage(statusResult.reason));
        setPill(nodes.state, 'Status unavailable', 'pill-bad');
        nodes.heartbeat.textContent = 'Could not read the bworker status.';
      }

      if (downloadsResult.status === 'fulfilled') {
        renderDownloads(downloadsResult.value
          && typeof downloadsResult.value === 'object'
          ? downloadsResult.value : {});
      } else {
        errors.push(errorMessage(downloadsResult.reason));
      }
    } catch (error) {
      // A malformed payload must not stop the refresh loop.
      if (generation === state.generation) errors.push(errorMessage(error));
    } finally {
      state.inFlight = false;
      state.remaining = state.interval;
      updateRefreshUI();
      if (generation === state.generation && errors.length) {
        const unique = Array.from(new Set(errors));
        const suffix = isRefreshActive()
          ? `Retrying every ${intervalText(state.interval)}.`
          : 'Refresh is paused.';
        setStatus(nodes.statusLine,
          `Refresh failed: ${unique[0]} ${suffix}`, 'bad');
      } else if (generation === state.generation
                 && nodes.statusLine.classList.contains('is-bad')) {
        setStatus(nodes.statusLine, '', '');
      }
      if (state.pendingPoll) {
        state.pendingPoll = false;
        pollOnce();
      }
    }
  }

  // ------------------------------------------------------------------- events
  document.addEventListener('change', async (event) => {
    if (!(event.target instanceof Element)) return;
    const priority = event.target.closest('[data-priority]');
    if (!priority) return;
    invalidatePoll();
    try {
      await patch(`/api/downloads/${encodeURIComponent(priority.dataset.priority)}`,
        { priority: Number(priority.value) || 0 });
      invalidatePoll();
      setStatus(nodes.statusLine, 'Queue priority updated.', 'ok');
      await pollOnce();
    } catch (error) {
      setStatus(nodes.statusLine, errorMessage(error), 'bad');
    }
  });

  document.addEventListener('click', async (event) => {
    if (!(event.target instanceof Element)) return;

    const cancel = event.target.closest('[data-cancel]');
    if (cancel) {
      invalidatePoll();
      try {
        await post(`/api/downloads/${encodeURIComponent(cancel.dataset.cancel)}/cancel`);
        invalidatePoll();
        setStatus(nodes.statusLine, 'Cancelling the download.', 'ok');
        await pollOnce();
      } catch (error) {
        setStatus(nodes.statusLine, errorMessage(error), 'bad');
      }
      return;
    }

    const retry = event.target.closest('[data-retry]');
    if (retry) {
      invalidatePoll();
      try {
        await post(`/api/downloads/${encodeURIComponent(retry.dataset.retry)}/retry`);
        invalidatePoll();
        setStatus(nodes.statusLine, 'Download queued again.', 'ok');
        await pollOnce();
      } catch (error) {
        setStatus(nodes.statusLine, errorMessage(error), 'bad');
      }
      return;
    }

    const remove = event.target.closest('[data-remove]');
    if (remove) {
      invalidatePoll();
      try {
        await del(`/api/downloads/${encodeURIComponent(remove.dataset.remove)}`);
        invalidatePoll();
        setStatus(nodes.statusLine, 'Download entry removed.', 'ok');
        await pollOnce();
      } catch (error) {
        setStatus(nodes.statusLine, errorMessage(error), 'bad');
      }
    }
  });

  nodes.pauseButton.addEventListener('click', toggleBworkerPause);
  nodes.resetButton.addEventListener('click', resetAll);
  nodes.refreshToggle.addEventListener('click', toggleManualPause);
  nodes.refreshPause.addEventListener('click', toggleManualPause);
  nodes.interval.addEventListener('change', onIntervalChange);
  nodes.log.addEventListener('scroll', onLogScroll, { passive: true });
  window.addEventListener('scroll', onScroll, { passive: true });
  document.addEventListener('visibilitychange', onVisibility);

  updateRefreshUI();
  state.wasActive = isRefreshActive();
  window.setInterval(tick, 1000);
  pollOnce();
})();
