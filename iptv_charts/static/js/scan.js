/* Library scan: trigger the task, wait for it, then review/import results. */

(() => {
  'use strict';

  const { $, get, post, setStatus, feedback } = Manager;

  const status = $('#scan-status');
  let polling = null;

  async function startScan(button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Scanning…';
    setStatus(status, 'Scanning the media folders…');
    try {
      await post('/api/tasks/scan_library/run');
      poll();
    } catch (error) {
      setStatus(status, error.message, 'bad');
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function poll() {
    window.clearTimeout(polling);
    try {
      const data = await get('/api/tasks');
      const task = (data.tasks || []).find((entry) => entry.name === 'scan_library');
      if (task && (task.status === 'queued' || task.status === 'running')) {
        setStatus(status, task.status === 'queued' ? 'Queued for the worker…' : 'Scanning…');
        polling = window.setTimeout(poll, 1500);
        return;
      }
      setStatus(status, task?.message || 'Scan finished.', task?.status === 'error' ? 'bad' : 'ok');
      window.setTimeout(() => window.location.reload(), 600);
    } catch (error) {
      setStatus(status, error.message, 'bad');
      polling = window.setTimeout(poll, 3000);
    }
  }

  $('#scan-now')?.addEventListener('click', (event) => startScan(event.currentTarget));

  $('#scan-import-all')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await post('/api/scan/import-all');
      setStatus(status,
        `Imported ${result.imported} file(s); ${result.failed} failed.`, 'ok');
      window.setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      setStatus(status, error.message, 'bad');
      button.disabled = false;
    }
  });

  document.addEventListener('click', async (event) => {
    const importButton = event.target.closest('[data-import]');
    if (importButton) {
      importButton.disabled = true;
      try {
        await post(`/api/scan/results/${importButton.dataset.import}/import`);
        setStatus(status, 'Imported.', 'ok');
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        importButton.disabled = false;
      }
      return;
    }

    const ignoreButton = event.target.closest('[data-ignore]');
    if (ignoreButton) {
      ignoreButton.disabled = true;
      try {
        await post(`/api/scan/results/${ignoreButton.dataset.ignore}/ignore`);
        setStatus(status, 'Ignored.', 'ok');
        window.setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        setStatus(status, error.message, 'bad');
        ignoreButton.disabled = false;
      }
    }
  });

  // A page reload during a scan should pick the scan back up.
  const initial = document.querySelector('#scan-status')?.textContent || '';
  if (initial.includes('Scanning') || initial.includes('Queued')) {
    poll();
  }
})();
