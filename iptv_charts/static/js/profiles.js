/* Quality profile editor: ordered tiers, source restrictions, audio and subs. */

(() => {
  'use strict';

  const { $, post, patch, del, setStatus, element, open, close } = Manager;

  const dialog = $('#profile-dialog');
  if (!dialog) return;

  const profiles = window.PROFILES || [];
  const resolutions = window.RESOLUTIONS || [];
  const sources = window.SOURCES || [];
  const languages = window.LANGUAGES || [];
  const nameInput = $('#profile-name');
  const tierList = $('#profile-tiers');
  const cutoff = $('#profile-cutoff');
  const upgrades = $('#profile-upgrades');
  const audioMode = $('#profile-audio-mode');
  const audioLanguages = $('#profile-audio-languages');
  const subtitleMode = $('#profile-subtitle-mode');
  const subtitleLanguages = $('#profile-subtitle-languages');
  const probeRequired = $('#profile-probe-required');
  const dialogStatus = $('#profile-dialog-status');
  const saveButton = $('#profile-save');
  const title = $('#profile-dialog-title');
  let editing = null;

  const sourceLabel = (value) => {
    const found = sources.find((entry) => entry[0] === value);
    return found ? found[1] : value;
  };

  const tierLabel = (resolution, chosenSources) => {
    if (!chosenSources.length) return resolution;
    return `${resolution} ${chosenSources.map(sourceLabel).join('/')}`;
  };

  const resolutionOptions = (selected) => {
    const select = document.createElement('select');
    select.className = 'tier-resolution';
    resolutions.forEach((resolution) => {
      const option = document.createElement('option');
      option.value = resolution;
      option.textContent = resolution;
      option.selected = resolution === selected;
      select.append(option);
    });
    return select;
  };

  const sourceOptions = (selected) => {
    const select = document.createElement('select');
    select.className = 'tier-sources';
    select.multiple = true;
    select.size = 4;
    select.title = 'Hold Ctrl (or Cmd) to choose multiple sources';
    sources.forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.selected = selected.includes(value);
      select.append(option);
    });
    return select;
  };

  function addTier(tier = {}) {
    const row = element('div', 'tier-row');
    const order = element('span', 'tier-order', '');
    const resolution = resolutionOptions(tier.resolution || '1080p');
    const chosenSources = Array.isArray(tier.sources) ? tier.sources.slice() : [];
    const sourceSelect = sourceOptions(chosenSources);
    const actions = element('span', 'tier-actions');

    const up = element('button', 'btn btn-sm btn-ghost', '↑');
    up.type = 'button';
    up.title = 'Move up (better)';
    up.addEventListener('click', () => {
      const previous = row.previousElementSibling;
      if (previous) {
        tierList.insertBefore(row, previous);
        refreshOrder();
      }
    });

    const down = element('button', 'btn btn-sm btn-ghost', '↓');
    down.type = 'button';
    down.title = 'Move down (worse)';
    down.addEventListener('click', () => {
      const next = row.nextElementSibling;
      if (next) {
        tierList.insertBefore(next, row);
        refreshOrder();
      }
    });

    const remove = element('button', 'btn btn-sm btn-danger', 'Remove');
    remove.type = 'button';
    remove.addEventListener('click', () => {
      if (tierList.children.length <= 1) {
        setStatus(dialogStatus, 'Keep at least one quality tier.', 'bad');
        return;
      }
      row.remove();
      refreshOrder();
    });

    resolution.addEventListener('change', refreshOrder);
    sourceSelect.addEventListener('change', refreshOrder);
    actions.append(up, down, remove);
    row.append(order, resolution, sourceSelect, actions);
    tierList.append(row);
    refreshOrder();
  }

  function refreshOrder() {
    Array.from(tierList.children).forEach((row, index) => {
      const order = row.querySelector('.tier-order');
      if (order) order.textContent = String(index + 1);
    });
    updateCutoff();
  }

  function readTiers() {
    return Array.from(tierList.children).map((row) => {
      const resolution = row.querySelector('.tier-resolution')?.value || '1080p';
      const chosen = Array.from(row.querySelector('.tier-sources')?.selectedOptions || [])
        .map((option) => option.value);
      return { resolution, sources: chosen };
    });
  }

  function updateCutoff() {
    const previous = cutoff.value;
    const tiers = readTiers();
    cutoff.replaceChildren();
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'No cutoff — upgrade forever';
    cutoff.append(none);
    tiers.forEach((tier) => {
      const label = tierLabel(tier.resolution, tier.sources);
      const option = document.createElement('option');
      option.value = label;
      option.textContent = label;
      cutoff.append(option);
    });
    const labels = tiers.map((tier) => tierLabel(tier.resolution, tier.sources));
    cutoff.value = labels.includes(previous) ? previous : (labels[0] || '');
  }

  function fillLanguageSelect(select, selected, includeOriginal = false) {
    select.replaceChildren();
    if (includeOriginal) {
      const original = document.createElement('option');
      original.value = 'original';
      original.textContent = 'Original language';
      original.selected = selected.includes('original');
      select.append(original);
    }
    languages.forEach(([code, label]) => {
      const option = document.createElement('option');
      option.value = code;
      option.textContent = `${label} (${code})`;
      option.selected = selected.includes(code);
      select.append(option);
    });
  }

  function selectedValues(select) {
    return Array.from(select.selectedOptions).map((option) => option.value);
  }

  function openNew() {
    editing = null;
    title.textContent = 'New profile';
    nameInput.value = '';
    tierList.replaceChildren();
    addTier({ resolution: '1080p', sources: [] });
    addTier({ resolution: '720p', sources: [] });
    cutoff.value = '';
    upgrades.checked = true;
    audioMode.value = 'any';
    subtitleMode.value = 'any';
    fillLanguageSelect(audioLanguages, []);
    fillLanguageSelect(subtitleLanguages, [], true);
    probeRequired.checked = true;
    setStatus(dialogStatus, '');
    open(dialog);
  }

  function openEdit(profile) {
    editing = profile.id;
    title.textContent = `Edit ${profile.name}`;
    nameInput.value = profile.name;
    tierList.replaceChildren();
    const tiers = Array.isArray(profile.qualities) && profile.qualities.length
      ? profile.qualities
      : (profile.resolutions || []).map((resolution) => ({ resolution, sources: [] }));
    tiers.forEach((tier) => addTier(tier));
    updateCutoff();
    cutoff.value = profile.cutoff || '';
    upgrades.checked = Boolean(profile.upgrades);
    audioMode.value = profile.audio_mode || 'any';
    subtitleMode.value = profile.subtitle_mode || 'any';
    fillLanguageSelect(audioLanguages, profile.audio_languages || []);
    fillLanguageSelect(subtitleLanguages, profile.subtitle_languages || [], true);
    probeRequired.checked = profile.probe_required !== false;
    setStatus(dialogStatus, '');
    open(dialog);
  }

  $('#new-profile')?.addEventListener('click', openNew);
  $('#profile-add-tier')?.addEventListener('click', () => addTier());

  document.addEventListener('click', (event) => {
    const edit = event.target.closest('[data-edit-profile]');
    if (edit) {
      const profile = profiles.find((entry) => String(entry.id) === String(edit.dataset.editProfile));
      if (profile) openEdit(profile);
      return;
    }
    const remove = event.target.closest('[data-delete-profile]');
    if (remove) deleteProfile(remove);
  });

  saveButton.addEventListener('click', async () => {
    const name = nameInput.value.trim();
    const tiers = readTiers();
    if (!name) {
      setStatus(dialogStatus, 'Give the profile a name.', 'bad');
      return;
    }
    if (!tiers.length) {
      setStatus(dialogStatus, 'Add at least one quality tier.', 'bad');
      return;
    }
    saveButton.disabled = true;
    try {
      const payload = {
        name,
        qualities: tiers.map((tier) => ({
          ...tier,
          name: tierLabel(tier.resolution, tier.sources),
        })),
        cutoff: cutoff.value || null,
        upgrades: upgrades.checked,
        audio_mode: audioMode.value,
        audio_languages: selectedValues(audioLanguages),
        subtitle_mode: subtitleMode.value,
        subtitle_languages: selectedValues(subtitleLanguages),
        probe_required: probeRequired.checked,
      };
      if (editing) await patch(`/api/quality-profiles/${editing}`, payload);
      else await post('/api/quality-profiles', payload);
      setStatus(dialogStatus, 'Saved — reloading…', 'ok');
      window.setTimeout(() => window.location.reload(), 400);
    } catch (error) {
      setStatus(dialogStatus, error.message, 'bad');
      saveButton.disabled = false;
    }
  });

  async function deleteProfile(button) {
    if (!window.confirm('Delete this quality profile?')) return;
    button.disabled = true;
    try {
      await del(`/api/quality-profiles/${button.dataset.deleteProfile}`);
      setStatus($('#profile-status'), 'Profile deleted.', 'ok');
      window.setTimeout(() => window.location.reload(), 400);
    } catch (error) {
      setStatus($('#profile-status'), error.message, 'bad');
      button.disabled = false;
    }
  }
})();
