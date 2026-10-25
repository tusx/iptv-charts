/* IPTV settings block on the /playlists manage page.
 *
 * The playlist route is owned by the shared app module, so the settings are
 * loaded from the section API instead of widening that page's render context.
 * External playback is off by default; the player controls stay visible but
 * disabled until the enable toggle is switched on.
 */

(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const root = $('[data-iptv-settings]');
  if (!root) return;

  const status = $('#iptv-settings-status');
  const commandStatus = $('#iptv-command-status');
  const toggle = $('#iptv-external-enabled');
  const fields = $('#iptv-player-fields');
  const choices = $('#iptv-player-choices');
  const command = $('#iptv-player-command');
  const hints = $('#iptv-player-hints');
  const stale = $('#iptv-stale-hours');
  const statePill = $('#iptv-player-state');
  const saveButton = $('#iptv-settings-save');
  const checkButton = $('#iptv-command-check');

  let customReady = false;
  let loaded = false;

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
      // Non-JSON error page; report the HTTP status below.
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

  function selectedPlayer() {
    const chosen = root.querySelector('input[name="preferred_player"]:checked');
    return chosen ? chosen.value : 'auto';
  }

  function addChoice(value, label, note, checked) {
    const wrapper = document.createElement('label');
    wrapper.className = 'choice';
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'preferred_player';
    input.value = value;
    input.checked = checked;
    const text = document.createElement('span');
    text.className = 'choice-label';
    text.textContent = label;
    wrapper.append(input, text);
    if (note) {
      const hint = document.createElement('span');
      hint.className = 'muted';
      hint.textContent = note;
      wrapper.append(hint);
    }
    choices.append(wrapper);
  }

  function renderPlayers(players, preferred) {
    choices.replaceChildren();
    const offered = players.map((found) => found.id);
    players.forEach((found) => {
      addChoice(found.id, found.label, '', preferred === found.id);
    });
    if (!offered.includes(preferred) && preferred !== 'custom' && preferred !== 'auto') {
      addChoice(preferred, 'Saved choice', `${preferred} — currently unavailable`,
                true);
    }
    addChoice('custom', 'Custom command',
              customReady ? 'the command below' : 'no command yet',
              preferred === 'custom');
    addChoice('auto', 'Automatic',
              players.length ? 'whichever is listed first' : 'nothing detected',
              preferred === 'auto');
  }

  function setEnabledState(enabled) {
    fields.disabled = !enabled;
    root.classList.toggle('is-inactive', !enabled);
    if (statePill) {
      statePill.textContent = enabled ? 'External player on' : 'External player off';
      statePill.classList.toggle('pill-ok', enabled);
      statePill.classList.toggle('pill-warn', !enabled);
    }
    if (!enabled) {
      setStatus(commandStatus, '');
    }
  }

  function bridgeHint(snapshot) {
    if (snapshot.bridged) {
      if (snapshot.bridge_alive) {
        return 'The player bridge is running, so these are the players installed on the host.';
      }
      return 'The player bridge is not running, so nothing can be started on the host right now. ' +
             'Start it with ./scripts/player-bridge.sh --daemon; the list is what it last reported.';
    }
    return 'These are the players found on this machine.';
  }

  async function load() {
    try {
      const snapshot = await sendJSON(root.dataset.loadUrl, 'GET');
      loaded = true;
      customReady = Boolean(snapshot.custom_ready);
      toggle.checked = Boolean(snapshot.external_player_enabled);
      stale.value = snapshot.stale_after_hours ?? '24';
      command.value = snapshot.player_command || '';
      renderPlayers(snapshot.players || [], snapshot.preferred_player || 'auto');
      setEnabledState(Boolean(snapshot.external_player_enabled));

      const notes = [bridgeHint(snapshot)];
      if (snapshot.bridge_outdated) {
        notes.push('That bridge is too old to report its players; restart it to choose one.');
      }
      if (!(snapshot.players || []).length && !snapshot.custom_ready) {
        notes.push('Nothing can play a stream externally yet. Install mpv or VLC, start the ' +
                   'player bridge, or give a command below.');
      }
      hints.textContent = notes.join(' ');
      saveButton.disabled = false;
    } catch (error) {
      setStatus(status, `Could not load IPTV settings: ${error.message}`, 'bad');
      saveButton.disabled = true;
    }
  }

  function payload() {
    return {
      player: {
        enabled: Boolean(toggle.checked),
        preferred_player: selectedPlayer(),
        player_command: command.value.trim(),
      },
      playlist: {
        stale_after_hours: Number(stale.value),
      },
    };
  }

  root.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!loaded) return;
    const body = payload();
    if (body.player.enabled && body.player.preferred_player === 'custom'
        && !body.player.player_command) {
      setStatus(status, 'Give the custom command first, or choose another player.', 'bad');
      command.focus();
      return;
    }
    setStatus(status, 'Saving IPTV settings…');
    try {
      const result = await withButton(saveButton, 'Saving…',
        () => sendJSON(root.dataset.saveUrl, 'POST', body));
      setEnabledState(Boolean(result.external_player_enabled));
      const bits = ['Saved'];
      if (result.player) bits.push(`playing via ${result.player.label}`);
      if (body.player.enabled && !result.player) {
        bits.push('no player is currently available');
      }
      if ((result.warnings || []).length) bits.push(result.warnings.join(' '));
      setStatus(status, `${bits.join(' — ')}.`, 'ok');
    } catch (error) {
      setStatus(status, error.message, 'bad');
    }
  });

  toggle.addEventListener('change', () => setEnabledState(toggle.checked));

  checkButton?.addEventListener('click', async () => {
    const text = command.value.trim();
    if (!text) {
      setStatus(commandStatus, 'Enter a command to check.', 'bad');
      return;
    }
    setStatus(commandStatus, 'Checking…');
    try {
      const result = await withButton(checkButton, 'Checking…',
        () => sendJSON(root.dataset.commandCheckUrl, 'POST', { player_command: text }));
      setStatus(commandStatus, result.message || 'Command syntax is valid.', 'ok');
    } catch (error) {
      setStatus(commandStatus, error.message, 'bad');
    }
  });

  load();
})();
