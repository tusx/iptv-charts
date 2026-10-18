/* Quality profile editor. */

(() => {
  'use strict';

  const { $, post, patch, del, setStatus, element, open, close, feedback } = Manager;

  const dialog = $('#profile-dialog');
  if (!dialog) return;

  const profiles = window.PROFILES || [];
  const resolutions = window.RESOLUTIONS || [];
  const nameInput = $('#profile-name');
  const choices = $('#profile-resolutions');
  const cutoff = $('#profile-cutoff');
  const upgrades = $('#profile-upgrades');
  const dialogStatus = $('#profile-dialog-status');
  const saveButton = $('#profile-save');
  const title = $('#profile-dialog-title');
  let editing = null;

  function renderChoices(selected) {
    choices.replaceChildren();
    resolutions.forEach((resolution) => {
      const label = element('label', 'check');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = resolution;
      input.checked = selected.includes(resolution);
      input.addEventListener('change', () => updateCutoff());
      label.append(input, document.createTextNode(` ${resolution}`));
      choices.append(label);
    });
    updateCutoff();
  }

  function checkedResolutions() {
    return Array.from(choices.querySelectorAll('input:checked')).map((input) => input.value);
  }

  function updateCutoff() {
    const previous = cutoff.value;
    const allowed = checkedResolutions();
    cutoff.replaceChildren();
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'No cutoff';
    cutoff.append(none);
    allowed.forEach((resolution) => {
      const option = document.createElement('option');
      option.value = resolution;
      option.textContent = resolution;
      cutoff.append(option);
    });
    cutoff.value = allowed.includes(previous) ? previous : (allowed[0] || '');
  }

  function openNew() {
    editing = null;
    title.textContent = 'New profile';
    nameInput.value = '';
    renderChoices(['1080p', '720p']);
    cutoff.value = '1080p';
    upgrades.checked = true;
    setStatus(dialogStatus, '');
    open(dialog);
  }

  function openEdit(profile) {
    editing = profile.id;
    title.textContent = `Edit ${profile.name}`;
    nameInput.value = profile.name;
    renderChoices(profile.resolutions || []);
    cutoff.value = profile.cutoff || '';
    upgrades.checked = Boolean(profile.upgrades);
    setStatus(dialogStatus, '');
    open(dialog);
  }

  $('#new-profile')?.addEventListener('click', openNew);

  document.addEventListener('click', (event) => {
    const edit = event.target.closest('[data-edit-profile]');
    if (edit) {
      const profile = profiles.find((entry) => String(entry.id) === String(edit.dataset.editProfile));
      if (profile) openEdit(profile);
      return;
    }
    const remove = event.target.closest('[data-delete-profile]');
    if (remove) {
      deleteProfile(remove);
    }
  });

  saveButton.addEventListener('click', async () => {
    const name = nameInput.value.trim();
    const chosen = checkedResolutions();
    if (!name) {
      setStatus(dialogStatus, 'Give the profile a name.', 'bad');
      return;
    }
    if (!chosen.length) {
      setStatus(dialogStatus, 'Choose at least one resolution.', 'bad');
      return;
    }
    saveButton.disabled = true;
    try {
      const payload = {
        name,
        resolutions: chosen,
        cutoff: cutoff.value || null,
        upgrades: upgrades.checked,
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
