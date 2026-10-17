/* Settings page: choosing the external player.
 *
 * One form, one POST, no state kept in the page beyond what the server sent --
 * the response reports what the choice resolves to, which is more useful than
 * echoing back what was typed. */

(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

  const form = $('#settings-form');
  if (!form) return;

  const status = $('#settings-status');
  const save = $('#settings-save');
  const previewCard = $('#settings-preview-card');
  const preview = $('#settings-preview');

  function setStatus(message, kind = '') {
    status.textContent = message;
    status.classList.toggle('is-bad', kind === 'bad');
    status.classList.toggle('is-ok', kind === 'ok');
  }

  function showCommand(argv) {
    if (!Array.isArray(argv) || !argv.length) {
      previewCard.hidden = true;
      return;
    }
    // Shown one argument per line: this is exactly what will be executed, and
    // a command that was meant to be one argument but got split in two is
    // invisible in a single line.
    preview.textContent = argv.join('\n');
    previewCard.hidden = false;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const chosen = form.querySelector('input[name="preferred_player"]:checked');
    const payload = {
      preferred_player: chosen ? chosen.value : 'auto',
      player_command: $('#player-command').value.trim(),
    };

    const original = save.textContent;
    save.disabled = true;
    save.textContent = 'Saving…';
    setStatus('');

    try {
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || result.ok === false) {
        throw new Error(result.error || `Request failed (HTTP ${response.status})`);
      }
      setStatus(result.player ? `Saved — playing via ${result.player.label}` : 'Saved', 'ok');
      showCommand(result.command);
    } catch (error) {
      setStatus(error.message, 'bad');
    } finally {
      save.disabled = false;
      save.textContent = original;
    }
  });

  // Picking "Custom command" without typing one is a dead end the server will
  // refuse; saying so here saves a round trip. Picking anything else clears the
  // complaint.
  form.addEventListener('change', () => {
    const chosen = form.querySelector('input[name="preferred_player"]:checked');
    const command = $('#player-command').value.trim();
    if (chosen && chosen.value === 'custom' && !command) {
      setStatus('Give a command below, then save.', 'bad');
    } else if (status.classList.contains('is-bad')) {
      setStatus('');
    }
  });
})();
