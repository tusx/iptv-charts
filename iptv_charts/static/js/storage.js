/* Storage page: read-only media tree with confirmed delete/empty actions.
 *
 * No framework and no build step, matching the rest of the app. Every value
 * from the API is inserted with textContent or dataset, never as HTML. */

(() => {
  'use strict';

  const {
    $, get, post, del, setStatus, element, formatBytes, timeAgo, open, close,
  } = Manager;

  const page = $('#storage-page');
  if (!page) return;

  const treeUrl = page.dataset.treeUrl;
  const deleteUrl = page.dataset.deleteUrl;
  const emptyUrl = page.dataset.emptyUrl;

  const rootSelect = $('#storage-root');
  const rootPath = $('#storage-root-path');
  const refreshButton = $('#storage-refresh');
  const emptyButton = $('#storage-empty-btn');
  const statusLine = $('#storage-status');
  const statsBox = $('#storage-stats');
  const treeBox = $('#storage-tree');

  const deleteDialog = $('#storage-delete-dialog');
  const deleteName = $('#storage-delete-name');
  const deletePath = $('#storage-delete-path');
  const deleteActiveBox = $('#storage-delete-active');
  const deleteActiveLabel = $('#storage-delete-active-label');
  const deleteActiveConfirm = $('#storage-delete-active-confirm');
  const deleteConfirm = $('#storage-delete-confirm');
  const deleteStatus = $('#storage-delete-status');
  const deleteSubmit = $('#storage-delete-submit');

  const emptyDialog = $('#storage-empty-dialog');
  const emptyName = $('#storage-empty-name');
  const emptyActiveBox = $('#storage-empty-active');
  const emptyActiveLabel = $('#storage-empty-active-label');
  const emptyActiveConfirm = $('#storage-empty-active-confirm');
  const emptyConfirm = $('#storage-empty-confirm');
  const emptyStatus = $('#storage-empty-status');
  const emptySubmit = $('#storage-empty-submit');

  const state = {
    root: 'all',
    data: null,
    pendingDelete: null,
    pendingEmpty: null,
    importing: false,
  };

  function selectedFolder() {
    if (!state.data || state.root === 'all') return null;
    return (state.data.folders || []).find((folder) => folder.key === state.root) || null;
  }

  function statTile(label, value, tone) {
    const tile = element('div', `storage-stat${tone ? ` storage-stat-${tone}` : ''}`);
    tile.append(element('span', 'storage-stat-label', label));
    tile.append(element('strong', 'storage-stat-value', value));
    return tile;
  }

  function renderStats() {
    if (!state.data) return;
    const folder = selectedFolder();
    const totals = folder ? {
      size: folder.size,
      files: folder.file_count,
      dirs: folder.dir_count,
      active: folder.active_count,
    } : (state.data.totals || {});
    statsBox.replaceChildren(
      statTile('Size', formatBytes(totals.size || 0)),
      statTile('Files', String(totals.files || 0)),
      statTile('Folders', String(totals.dirs || 0)),
      statTile('Active downloads', String(totals.active || 0),
        totals.active ? 'active' : ''),
    );
    rootPath.textContent = folder
      ? (folder.path || state.data.root)
      : state.data.root;
  }

  function renderStatusBadge(active) {
    const status = active.cancel_requested ? 'cancelling' : active.status;
    const fraction = active.fraction;
    const percent = fraction === null || fraction === undefined
      ? '' : ` ${Math.round(fraction * 100)}%`;
    const wrap = element('span', 'storage-active-wrap');
    const badge = element(
      'span',
      `badge badge-${active.status === 'importing' ? 'importing' : active.status}`,
      `${status}${percent}`,
    );
    badge.title = [active.title || 'Download', `#${active.id}`]
      .filter(Boolean).join(' · ');
    wrap.append(badge);
    if (fraction !== null && fraction !== undefined) {
      const bar = element('span', 'storage-mini-bar');
      const fill = element('span', 'storage-mini-bar-fill');
      fill.style.width = `${Math.max(0, Math.min(100, fraction * 100)).toFixed(1)}%`;
      bar.append(fill);
      wrap.append(bar);
    }
    return wrap;
  }

  function timestamp(value) {
    if (!value) return 'unknown date';
    return new Date(Number(value) * 1000).toLocaleString();
  }

  function renderFile(node) {
    const row = element('div', 'storage-file');
    const main = element('div', 'storage-file-main');
    const name = element('span', 'storage-file-name', node.name);
    name.title = node.path;
    main.append(name);

    const details = [];
    if (node.kind === 'symlink') {
      details.push('symbolic link — not followed');
    } else if (node.kind === 'other') {
      details.push('not a regular file');
    } else if (node.kind === 'unreadable') {
      details.push('unreadable');
    }
    if (node.kind === 'file') {
      const when = element('span', 'storage-file-sub',
        `${formatBytes(node.size)} · ${timeAgo(node.modified)}`);
      when.title = timestamp(node.modified);
      main.append(when);
    } else {
      main.append(element('span', 'storage-file-sub', details.join(' · ')));
    }
    if (node.error) main.append(element('span', 'storage-file-sub', node.error));

    const badges = element('div', 'storage-file-badges');
    if (node.kind === 'symlink') {
      badges.append(element('span', 'badge badge-muted', 'symlink'));
    } else if (node.kind === 'other' || node.kind === 'unreadable') {
      badges.append(element('span', 'badge badge-muted', node.kind));
    }
    if (node.active) badges.append(renderStatusBadge(node.active));

    const actions = element('div', 'storage-file-actions');
    if (node.kind === 'file') {
      const remove = element('button', 'btn btn-sm btn-danger', 'Delete');
      remove.type = 'button';
      remove.dataset.deletePath = node.path;
      remove.dataset.deleteName = node.name;
      remove.dataset.active = node.active ? '1' : '0';
      if (node.active) {
        remove.dataset.activeLabel = [node.active.title || 'download',
          node.active.status].join(' · ');
        remove.dataset.activeStatus = node.active.status;
      }
      actions.append(remove);
    }

    row.append(main, badges, actions);
    return row;
  }

  function renderDirectory(node, depth) {
    const details = element('details', 'storage-dir');
    // Top-level folders start open; deeper folders start collapsed so a large
    // library is still fast to scan on a phone.
    if (depth <= 1) details.open = true;

    const summary = element('summary', 'storage-dir-summary');
    const name = element('span', 'storage-dir-name', node.name);
    name.title = node.path;
    summary.append(name);
    const counts = [];
    counts.push(`${node.file_count || 0} file${node.file_count === 1 ? '' : 's'}`);
    if (node.dir_count) {
      counts.push(`${node.dir_count} folder${node.dir_count === 1 ? '' : 's'}`);
    }
    counts.push(formatBytes(node.size || 0));
    summary.append(element('span', 'storage-dir-meta', counts.join(' · ')));

    const body = element('div', 'storage-children');
    if (node.error) body.append(element('p', 'note note-warn', node.error));
    if (!(node.children || []).length) {
      if (!node.error) body.append(element('p', 'muted small', 'Empty folder.'));
    } else {
      node.children.forEach((child) => {
        body.append(child.kind === 'dir'
          ? renderDirectory(child, depth + 1)
          : renderFile(child));
      });
    }
    details.append(summary, body);
    return details;
  }

  function renderFolder(folder) {
    const section = element('section', 'storage-folder');
    const head = element('div', 'storage-folder-head');
    const titles = element('div', 'storage-folder-titles');
    titles.append(element('h2', 'storage-folder-name', folder.label));
    if (folder.path) titles.append(element('span', 'muted small', folder.path));
    head.append(titles);

    const badgeBox = element('div', 'storage-folder-badges');
    if (folder.active_count) {
      badgeBox.append(element('span', 'badge badge-downloading',
        `${folder.active_count} active`));
    }
    if (folder.error) badgeBox.append(element('span', 'badge badge-failed', 'unavailable'));
    head.append(badgeBox);
    section.append(head);

    const body = element('div', 'storage-folder-body');
    if (folder.error) {
      body.append(element('p', 'note note-warn', folder.error));
    } else if (!(folder.children || []).length) {
      body.append(element('p', 'muted', 'This folder is empty.'));
    } else {
      folder.children.forEach((child) => {
        body.append(child.kind === 'dir'
          ? renderDirectory(child, 1)
          : renderFile(child));
      });
    }
    section.append(body);
    return section;
  }

  function render() {
    if (!state.data) return;
    renderStats();
    treeBox.replaceChildren();

    const errors = state.data.errors || [];
    if (errors.length) {
      const note = element('div', 'note note-warn');
      note.append(element('strong', '', 'Some entries could not be read.'));
      errors.slice(0, 8).forEach((message) => {
        note.append(element('p', 'small', message));
      });
      treeBox.append(note);
    }

    const folder = selectedFolder();
    if (folder) {
      treeBox.append(renderFolder(folder));
      return;
    }
    (state.data.folders || []).forEach((item) => treeBox.append(renderFolder(item)));
  }

  function syncRootOptions() {
    (state.data.folders || []).forEach((folder) => {
      const option = rootSelect.querySelector(`option[value="${folder.key}"]`);
      if (!option) return;
      option.textContent = `${folder.label}${folder.error ? ' (unavailable)' : ''}`;
      option.dataset.label = folder.label;
    });
  }

  async function refresh() {
    refreshButton.disabled = true;
    setStatus(statusLine, 'Refreshing…');
    try {
      const data = await get(treeUrl);
      state.data = data;
      syncRootOptions();
      render();
      const totals = data.totals || {};
      const parts = [`${totals.files || 0} files`, formatBytes(totals.size || 0)];
      if (totals.active) parts.push(`${totals.active} active`);
      setStatus(statusLine, `Refreshed ${new Date().toLocaleTimeString()} · ${parts.join(' · ')}`, 'ok');
    } catch (error) {
      setStatus(statusLine, error.message, 'bad');
    } finally {
      refreshButton.disabled = false;
    }
  }

  function updateDeleteSubmit() {
    if (!state.pendingDelete) {
      deleteSubmit.disabled = true;
      return;
    }
    const activeOk = !state.pendingDelete.active || deleteActiveConfirm.checked;
    deleteSubmit.disabled = state.importing
      || !deleteConfirm.checked || !activeOk;
  }

  function openDeleteDialog(button) {
    state.pendingDelete = {
      path: button.dataset.deletePath,
      name: button.dataset.deleteName,
      active: button.dataset.active === '1',
      activeLabel: button.dataset.activeLabel || '',
      activeStatus: button.dataset.activeStatus || '',
    };
    state.importing = state.pendingDelete.activeStatus === 'importing';

    deleteName.textContent = state.pendingDelete.name;
    deletePath.textContent = state.pendingDelete.path;
    deleteActiveBox.hidden = !state.pendingDelete.active;
    deleteActiveLabel.textContent = state.pendingDelete.activeLabel;
    deleteActiveConfirm.checked = false;
    deleteConfirm.checked = false;
    if (state.importing) {
      setStatus(deleteStatus,
        'This download is importing. Wait for the import to finish before deleting the file.',
        'bad');
    } else if (state.pendingDelete.active) {
      setStatus(deleteStatus,
        'Deleting this staging file also cancels its download.', 'bad');
    } else {
      setStatus(deleteStatus, '');
    }
    updateDeleteSubmit();
    open(deleteDialog);
  }

  async function submitDelete() {
    const pending = state.pendingDelete;
    if (!pending) return;
    deleteSubmit.disabled = true;
    setStatus(deleteStatus, 'Deleting…');
    try {
      const result = await del(deleteUrl, {
        path: pending.path,
        confirm: true,
        confirm_active: pending.active ? deleteActiveConfirm.checked : false,
      });
      close(deleteDialog);
      const cancelled = (result.cancelled_downloads || []).length;
      const cleared = result.library || {};
      const touched = (cleared.movies || 0) + (cleared.episodes || 0)
        + (cleared.alternates || 0);
      let message = `Deleted ${pending.name} (${formatBytes(result.size)}).`;
      if (cancelled) message += ' The download was cancelled.';
      if (touched) message += ' The library entry is now marked missing.';
      setStatus(statusLine, message, 'ok');
      state.pendingDelete = null;
      await refresh();
    } catch (error) {
      setStatus(deleteStatus, error.message, 'bad');
      deleteSubmit.disabled = false;
    }
  }

  function updateEmptySubmit() {
    if (!state.pendingEmpty) {
      emptySubmit.disabled = true;
      return;
    }
    const activeOk = !state.pendingEmpty.active || emptyActiveConfirm.checked;
    emptySubmit.disabled = !emptyConfirm.checked || !activeOk;
  }

  function openEmptyDialog() {
    const folder = selectedFolder();
    if (!folder) return;
    state.pendingEmpty = {
      key: folder.key,
      label: folder.label,
      active: Boolean(folder.active_count),
      activeCount: folder.active_count || 0,
    };
    emptyName.textContent = folder.label;
    emptyActiveBox.hidden = !folder.active_count;
    emptyActiveLabel.textContent = `${folder.active_count} active download`
      + `${folder.active_count === 1 ? '' : 's'}`;
    emptyActiveConfirm.checked = false;
    emptyConfirm.checked = false;
    setStatus(emptyStatus, folder.active_count
      ? 'Emptying this folder cancels its active downloads first.' : '');
    updateEmptySubmit();
    open(emptyDialog);
  }

  async function submitEmpty() {
    const pending = state.pendingEmpty;
    if (!pending) return;
    emptySubmit.disabled = true;
    setStatus(emptyStatus, 'Emptying…');
    try {
      const result = await post(emptyUrl, {
        folder: pending.key,
        confirm: true,
        confirm_active: pending.active ? emptyActiveConfirm.checked : false,
      });
      close(emptyDialog);
      let message = `Emptied ${pending.label}: ${result.deleted_files} files, `
        + `${formatBytes(result.bytes_freed)} freed.`;
      if ((result.cancelled_downloads || []).length) {
        message += ' Active downloads were cancelled.';
      }
      if ((result.errors || []).length) {
        message += ` ${result.errors.length} entr`
          + `${result.errors.length === 1 ? 'y' : 'ies'} could not be removed.`;
      }
      setStatus(statusLine, message, (result.errors || []).length ? 'bad' : 'ok');
      state.pendingEmpty = null;
      await refresh();
    } catch (error) {
      setStatus(emptyStatus, error.message, 'bad');
      // A stale tree may not have shown the active-download warning. Reveal it
      // so the user can make the second confirmation the server asked for.
      if (/active downloads/i.test(error.message)) {
        emptyActiveBox.hidden = false;
        if (!emptyActiveLabel.textContent) {
          emptyActiveLabel.textContent = 'active downloads in this folder';
        }
        pending.active = true;
      }
      updateEmptySubmit();
    }
  }

  rootSelect.addEventListener('change', () => {
    state.root = rootSelect.value;
    emptyButton.disabled = state.root === 'all';
    render();
  });

  refreshButton.addEventListener('click', refresh);
  emptyButton.addEventListener('click', openEmptyDialog);

  deleteConfirm.addEventListener('change', updateDeleteSubmit);
  deleteActiveConfirm.addEventListener('change', updateDeleteSubmit);
  deleteSubmit.addEventListener('click', submitDelete);

  emptyConfirm.addEventListener('change', updateEmptySubmit);
  emptyActiveConfirm.addEventListener('change', updateEmptySubmit);
  emptySubmit.addEventListener('click', submitEmpty);

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-delete-path]');
    if (button) openDeleteDialog(button);
  });

  emptyButton.disabled = true;
  refresh();
})();
