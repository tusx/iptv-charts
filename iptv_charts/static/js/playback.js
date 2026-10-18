/* Browser playback, in a modal on the browse page.
 *
 * Deliberately thin. A plain <video> element plays what the browser can play,
 * and hls.js covers the one format it cannot handle on its own -- HLS, which is
 * what live channels usually are and what some providers serve for VOD too.
 * Everything else is left to the browser: if it cannot decode a stream it says
 * so in its own controls, and the dialog keeps the external player next to it as
 * the way that actually works for MPEG-TS and for AC-3 audio.
 *
 * `generation` guards the one real hazard of reusing a dialog: a stream that
 * resolves after it was closed must not start playing to a hidden element. A
 * closed <dialog> is display:none, which does *not* stop media. */

(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

  const dialog = $('#player-dialog');
  if (!dialog) return;

  const video = $('#video');
  const streamNote = $('#stream-note');
  const title = $('#player-title');
  const poster = $('#player-poster');
  const metaList = $('#player-meta');
  const about = $('#player-about');
  const plotLine = $('#player-plot');

  const episodesSection = $('#episodes');
  const episodeList = $('#episode-list');

  let current = null;        // {id, kind} of the item on screen
  let detail = null;         // that item's card data, merged with the stream fetch
  let stream = null;         // {url, media_type, ...} once it resolves
  let hls = null;
  let generation = 0;
  let openedAt = 0;

  const stale = (mine) => mine !== generation;

  function teardown() {
    if (hls) {
      try { hls.destroy(); } catch { /* already gone */ }
      hls = null;
    }
    video.removeAttribute('src');
    video.load();
  }

  function play() {
    // A rejected play() is the autoplay policy asking for a click, and the
    // browser's own controls are that click. Nothing to report.
    const attempt = video.play();
    if (attempt && typeof attempt.catch === 'function') attempt.catch(() => {});
  }

  // The note under the meta column is the dialog's status line: the stream
  // format while playing, and the reason when the player could not start. A
  // tooltip alone is too quiet for "the click did nothing".
  function setNote(text, bad = false) {
    if (!streamNote) return;
    streamNote.textContent = text;
    streamNote.classList.toggle('is-bad', bad);
  }

  function start(mine) {
    if (!stream || stale(mine)) return;
    teardown();

    if (stream.media_type === 'hls' && window.Hls && window.Hls.isSupported()) {
      hls = new window.Hls({
        // A live stream with a tiny buffer stalls constantly; the defaults are
        // tuned for VOD.
        lowLatencyMode: false,
        enableWorker: true,
      });
      hls.on(window.Hls.Events.ERROR, (_event, data) => {
        // Non-fatal errors are recovered from internally. A fatal one means
        // hls.js has given up: drop it and leave the element to the browser,
        // which shows its own error, with the external player beside it.
        if (!data.fatal) return;
        teardown();
      });
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        if (!stale(mine)) play();
      });
      hls.loadSource(stream.url);
      hls.attachMedia(video);
      return;
    }

    video.src = stream.url;
    play();
  }

  // --------------------------------------------------------------- loading
  async function loadStream() {
    const mine = generation;
    const item = current;
    if (!item) return;

    let payload;
    try {
      const response = await fetch(`/api/items/${item.id}/stream`);
      payload = await response.json();
    } catch {
      return;   // the browser will show whatever it shows; the dialog stays usable
    }
    if (stale(mine)) return;   // closed, or another item opened, meanwhile
    if (!payload.ok) {
      setNote(payload.error || '', true);
      return;
    }

    stream = payload;
    // The card carries everything except cast, director and release date, which
    // only this endpoint knows.
    detail = { ...detail, ...payload };
    renderAbout();
    setNote(`Playing as ${String(stream.media_type).toUpperCase()}.`);
    start(mine);
  }

  // ---------------------------------------------------------------- details
  function metaRow(label, value, { mono = false } = {}) {
    const row = document.createElement('div');
    const term = document.createElement('dt');
    term.textContent = label;
    const definition = document.createElement('dd');
    if (mono) definition.className = 'mono';
    definition.textContent = value;
    row.append(term, definition);
    return row;
  }

  function renderMeta() {
    const item = detail || {};
    metaList.replaceChildren();
    const rows = [
      ['Type', item.kind],
      ['Category', item.category_name],
      ['Genre', item.genre],
      ['Duration', item.duration],
      ['Rating', item.rating ? `★ ${item.rating}` : ''],
      ['EPG id', item.epg_channel_id, true],
      ['Catch-up', item.tv_archive ? 'available' : ''],
      ['Source', window.SOURCE_NAME],
    ];
    for (const [label, value, mono] of rows) {
      if (value) metaList.appendChild(metaRow(label, value, { mono }));
    }
  }

  function line(name, value) {
    const row = about.querySelector(`[data-line="${name}"]`);
    if (!row) return;
    row.hidden = !value;
    row.querySelector('span').textContent = value || '';
  }

  function renderAbout() {
    const item = detail || {};
    const plot = (item.plot || '').trim();
    const cast = (item.cast_list || '').trim();
    const director = (item.director || '').trim();
    const released = (item.release_date || '').trim();

    about.hidden = !(plot || cast || director || released);
    plotLine.textContent = plot;
    plotLine.hidden = !plot;
    line('cast', cast);
    line('director', director);
    line('released', released);
  }

  function renderPoster(url) {
    poster.hidden = !url;
    if (url) poster.src = url;
    else poster.removeAttribute('src');
  }

  // --------------------------------------------------------------- actions
  async function copyLink(button) {
    const original = button.textContent;
    // Read at click time: the dialog may be showing something else by the time
    // this resolves, and copying the wrong stream would be silent.
    const item = current;
    let url = stream?.url;
    if (!url && item) {
      try {
        const payload = await (await fetch(`/api/items/${item.id}/stream`)).json();
        url = payload.url;
      } catch { /* handled below */ }
    }
    if (!url) {
      button.textContent = '✕';
      setTimeout(() => { button.textContent = original; }, 1500);
      return;
    }

    const ok = await writeClipboard(url);
    button.textContent = ok ? '✓ Copied' : '✕ Failed';
    button.title = ok ? url : 'Clipboard unavailable — the link is in the console';
    if (!ok) console.info('Stream URL:', url);
    setTimeout(() => {
      button.textContent = original;
      button.title = '';
    }, 1500);
  }

  async function writeClipboard(text) {
    // navigator.clipboard needs a secure context. http://127.0.0.1 and
    // http://localhost both qualify, so this normally just works; the textarea
    // route covers anything unusual.
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch { /* fall through */ }

    try {
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.setAttribute('readonly', '');
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      const ok = document.execCommand('copy');
      scratch.remove();
      return ok;
    } catch {
      return false;
    }
  }

  async function playExternal(button, episodeId) {
    const item = current;
    if (!item) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = '…';

    // An episode id is sent, not a URL: the server checks it really belongs
    // to this series before launching anything.
    const endpoint = episodeId
      ? `/api/items/${item.id}/episodes/${encodeURIComponent(episodeId)}/play`
      : `/api/items/${item.id}/play`;

    try {
      const payload = await (await fetch(endpoint, { method: 'POST' })).json();
      if (!payload.ok) throw new Error(payload.error);
      button.textContent = '✓ Sent';
      button.title = `Playing in ${payload.player}`;
      setNote(`Opened in ${payload.player}.`);
    } catch (error) {
      // The endpoint waits for the host bridge to report, so this is usually
      // the player's own reason -- "HTTP error 500", a missing program --
      // rather than a generic failure.
      button.textContent = '✕';
      button.title = error.message;
      setNote(error.message, true);
    }
    setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, 1500);
  }

  // -------------------------------------------------------------- episodes
  async function loadEpisodes() {
    const mine = generation;
    const item = current;
    if (!item || !episodesSection || !episodeList) return;

    let payload;
    try {
      payload = await (await fetch(`/api/items/${item.id}/episodes`)).json();
    } catch {
      // Episodes are an enhancement; the main stream still plays without them.
      return;
    }
    if (stale(mine)) return;

    if (!payload.ok) {
      episodesSection.hidden = false;
      const note = document.createElement('p');
      note.className = 'muted';
      note.textContent = payload.error;
      episodeList.appendChild(note);
      return;
    }

    for (const season of payload.seasons) {
      const heading = document.createElement('h3');
      heading.className = 'season-head';
      heading.textContent = `Season ${season.season}`;
      episodeList.appendChild(heading);

      const grid = document.createElement('div');
      grid.className = 'episode-grid';

      for (const episode of season.episodes) {
        const button = document.createElement('button');
        button.className = 'episode-btn';
        button.type = 'button';

        const number = document.createElement('span');
        number.className = 'episode-num';
        number.textContent = episode.number || '•';

        const label = document.createElement('span');
        label.className = 'episode-title';
        label.textContent = episode.title;

        button.append(number, label);

        const actions = document.createElement('span');
        actions.className = 'episode-actions';

        const playBtn = document.createElement('span');
        playBtn.className = 'episode-action';
        playBtn.textContent = '▶';
        playBtn.title = 'Play this episode';
        playBtn.addEventListener('click', (event) => {
          event.stopPropagation();
          stream = { url: episode.url, media_type: 'file' };
          start(generation);
        });
        actions.appendChild(playBtn);

        const copyBtn = document.createElement('span');
        copyBtn.className = 'episode-action';
        copyBtn.textContent = '⧉';
        copyBtn.title = 'Copy this episode\'s link';
        copyBtn.addEventListener('click', (event) => {
          event.stopPropagation();
          const glyph = copyBtn.textContent;
          copyBtn.textContent = '✓';
          if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(episode.url).catch(() => {});
          }
          setTimeout(() => { copyBtn.textContent = glyph; }, 1200);
        });
        actions.appendChild(copyBtn);

        if (window.EXTERNAL_PLAYER_AVAILABLE) {
          const mpvBtn = document.createElement('span');
          mpvBtn.className = 'episode-action';
          mpvBtn.textContent = '⤴';
          mpvBtn.title = `Play this episode in ${window.EXTERNAL_PLAYER_LABEL || 'the external player'}`;
          mpvBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            playExternal(mpvBtn, episode.id);
          });
          actions.appendChild(mpvBtn);
        }

        button.appendChild(actions);
        grid.appendChild(button);
      }

      episodeList.appendChild(grid);
    }

    episodesSection.hidden = false;
  }

  // ------------------------------------------------------------------ modal
  function open(item) {
    if (!item || !item.id || !item.kind) return;

    // Everything in flight belongs to the item that was on screen until now.
    generation += 1;
    current = { id: item.id, kind: item.kind };
    detail = item;
    stream = null;

    // The dialog is reused, so clear anything the last item left behind.
    if (episodeList) episodeList.replaceChildren();
    if (episodesSection) episodesSection.hidden = item.kind !== 'series';
    renderPoster(item.logo);
    renderMeta();
    renderAbout();
    setNote('');
    title.textContent = item.name || '';
    video.poster = item.logo || '';

    if (!dialog.open) dialog.showModal();
    // Stamped on every open, not just the first, so the backdrop-click guard
    // below always measures from the item that is actually showing.
    openedAt = performance.now();
    // Not the close button, and not the video: Enter should not dismiss the
    // player, and focus belongs on the dialog until the user moves it.
    dialog.focus();

    loadStream();
    if (item.kind === 'series') loadEpisodes();
  }

  window.openPlayer = open;

  // ----------------------------------------------------------------- wiring
  // A broken logo must not take the element with it -- the poster is reused
  // for every item that follows.
  poster?.addEventListener('error', () => renderPoster(''));

  // Closing has to stop playback: a <dialog> that is merely display:none keeps
  // playing audio, and the element is reused for the next item.
  dialog.addEventListener('close', () => {
    generation += 1;
    current = null;
    detail = null;
    stream = null;
    teardown();
  });

  // Clicking the dim area closes the player, as it does for the Add-playlist
  // dialog -- but not in the first moments: the second click of a double-click
  // on a poster lands here, and would close the modal the instant it opened.
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog && performance.now() - openedAt > 350) dialog.close();
  });

  $('#player-close-btn')?.addEventListener('click', () => dialog.close());
  $('#player-done-btn')?.addEventListener('click', () => dialog.close());
  $('#copy-btn')?.addEventListener('click', (event) => copyLink(event.currentTarget));
  $('#external-btn')?.addEventListener('click', (event) => playExternal(event.currentTarget));
})();
