/* Weighted quality profile builder.
 *
 * A profile has one mandatory resolution, an ordered list of weighted
 * quality/source options, and collapsible optional sections. The dialog
 * computes the same maximum score algebra as the Python engine so the
 * "Target N / Max M" line stays live while weights change. */

(() => {
  'use strict';

  const { $, $$, post, patch, del, setStatus, element, open, close } = Manager;

  const dialog = $('#profile-dialog');
  if (!dialog) return;

  const schema = window.PROFILE_SCHEMA || {};
  const profiles = window.PROFILES || [];
  const resolutions = schema.resolutions || [];
  const qualityOptions = schema.quality_options || [];
  const defaultQualities = schema.default_quality_options || [];
  const sections = schema.sections || [];
  const qualityMeta = new Map(qualityOptions.map((entry) => [entry.value, entry]));
  const sectionMeta = new Map(sections.map((entry) => [entry.key, entry]));

  const nameInput = $('#profile-name');
  const resolutionSelect = $('#profile-resolution');
  const resolutionWeight = $('#profile-resolution-weight');
  const qualityList = $('#profile-quality-list');
  const qualitySelect = $('#profile-quality-select');
  const qualityAdd = $('#profile-quality-add');
  const targetInput = $('#profile-target');
  const maxOutput = $('#profile-max-score');
  const scoreLine = $('#profile-score-line');
  const upgrades = $('#profile-upgrades');
  const dialogStatus = $('#profile-dialog-status');
  const saveButton = $('#profile-save');
  const dialogTitle = $('#profile-dialog-title');

  const state = { qualities: [], sections: {} };
  let editing = null;
  let targetTouched = false;
  let dragIndex = null;

  const clampWeight = (value, fallback = 1) => {
    const number = Math.round(Number(value));
    if (!Number.isFinite(number)) return fallback;
    return Math.min(1000, Math.max(1, number));
  };

  const sectionOptions = (key) => sectionMeta.get(key)?.options || [];
  const metaFor = (key, value) => {
    if (key === 'quality_options') return qualityMeta.get(value);
    return sectionOptions(key).find((entry) => entry.value === value);
  };
  const labelFor = (key, value) =>
    metaFor(key, value)?.label || String(value || '');

  function defaultResolution() {
    const preferred = profiles.find(
      (profile) => profile.builtin_key === window.DEFAULT_PROFILE_KEY
    ) || profiles.find((profile) => profile.builtin_key === '1080p');
    return preferred?.resolution || '1080p';
  }

  function populateResolutions(selected) {
    resolutionSelect.textContent = '';
    resolutions.forEach((resolution) => {
      const option = element('option', '', resolution);
      option.value = resolution;
      option.selected = resolution === selected;
      resolutionSelect.append(option);
    });
  }

  function weightInput(value, onChange) {
    const input = element('input', 'option-weight');
    input.type = 'number';
    input.min = '1';
    input.max = '1000';
    input.step = '1';
    input.value = String(clampWeight(value, 1));
    input.setAttribute('aria-label', 'Weight');
    input.addEventListener('change', () => {
      const next = clampWeight(input.value, Number(input.dataset.last) || 1);
      input.value = String(next);
      input.dataset.last = String(next);
      onChange(next);
    });
    input.dataset.last = input.value;
    return input;
  }

  function moveButton(text, title, disabled, handler) {
    const button = element('button', 'btn btn-sm btn-ghost', text);
    button.type = 'button';
    button.title = title;
    button.setAttribute('aria-label', title);
    button.disabled = disabled;
    button.addEventListener('click', handler);
    return button;
  }

  function optionRow(key, list, item, index, onChange) {
    const row = element('div', 'option-row');
    row.dataset.index = String(index);
    row.dataset.value = item.value;
    const reorder = key === 'quality_options';

    if (reorder) {
      row.draggable = true;
      const handle = element('span', 'drag-handle', '⠿');
      handle.title = 'Drag to reorder';
      handle.setAttribute('aria-hidden', 'true');
      row.append(handle);
      row.addEventListener('dragstart', (event) => {
        dragIndex = index;
        row.classList.add('is-dragging');
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData('text/plain', item.value);
        }
      });
      row.addEventListener('dragend', () => {
        dragIndex = null;
        row.classList.remove('is-dragging');
      });
      row.addEventListener('dragover', (event) => {
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
      });
      row.addEventListener('drop', (event) => {
        event.preventDefault();
        if (dragIndex === null || dragIndex === index) return;
        const moved = list.splice(dragIndex, 1)[0];
        list.splice(index, 0, moved);
        dragIndex = null;
        onChange.reorder();
      });
    }

    row.append(element('span', 'option-label', item.label || labelFor(key, item.value)));
    row.append(weightInput(item.weight, (weight) => {
      item.weight = weight;
      onChange.weights();
    }));

    if (reorder) {
      const up = moveButton('↑', 'Move up', index === 0, () => {
        if (index === 0) return;
        const moved = list.splice(index, 1)[0];
        list.splice(index - 1, 0, moved);
        onChange.reorder();
      });
      const down = moveButton('↓', 'Move down', index === list.length - 1, () => {
        if (index >= list.length - 1) return;
        const moved = list.splice(index, 1)[0];
        list.splice(index + 1, 0, moved);
        onChange.reorder();
      });
      row.append(up, down);
    }

    const remove = element('button', 'btn btn-sm btn-danger', 'Remove');
    remove.type = 'button';
    remove.setAttribute('aria-label', `Remove ${item.label || item.value}`);
    remove.addEventListener('click', () => {
      const current = list.findIndex((entry) => entry.value === item.value);
      if (current >= 0) list.splice(current, 1);
      onChange.remove();
    });
    row.append(remove);
    return row;
  }

  function renderQualityList() {
    qualityList.textContent = '';
    state.qualities.forEach((item, index) => {
      qualityList.append(optionRow(
        'quality_options', state.qualities, item, index, {
          weights: refreshScores,
          remove: () => {
            renderQualityList();
            populateQualitySelect();
            refreshScores();
          },
          reorder: () => {
            renderQualityList();
            populateQualitySelect();
            refreshScores();
          },
        }
      ));
    });
    if (!state.qualities.length) {
      qualityList.append(element('p', 'hint', 'Add at least one quality/source option.'));
    }
  }

  function populateQualitySelect() {
    qualitySelect.textContent = '';
    const remaining = qualityOptions.filter(
      (entry) => !state.qualities.some((item) => item.value === entry.value)
    );
    if (!remaining.length) {
      const option = element('option', '', 'All quality options added');
      option.value = '';
      qualitySelect.append(option);
      qualitySelect.disabled = true;
      qualityAdd.disabled = true;
      return;
    }
    qualitySelect.disabled = false;
    qualityAdd.disabled = false;
    remaining.forEach((entry) => {
      const option = element('option', '', entry.label);
      option.value = entry.value;
      qualitySelect.append(option);
    });
  }

  function addQuality() {
    const value = qualitySelect.value;
    if (!value || state.qualities.some((item) => item.value === value)) return;
    const meta = qualityMeta.get(value);
    state.qualities.push({
      value,
      label: meta?.label || value,
      weight: clampWeight(meta?.weight, 10),
    });
    renderQualityList();
    populateQualitySelect();
    refreshScores();
  }

  function renderSection(key) {
    const list = $('[data-option-list="' + key + '"]', dialog);
    const count = $('[data-section-count="' + key + '"]', dialog);
    const details = $('[data-section="' + key + '"]', dialog);
    if (!list) return;
    const items = state.sections[key] || [];
    list.textContent = '';
    items.forEach((item, index) => {
      list.append(optionRow(key, items, item, index, {
        weights: refreshScores,
        remove: () => {
          renderSection(key);
          populateSectionSelect(key);
          refreshScores();
        },
        reorder: () => {},
      }));
    });
    if (!items.length) {
      list.append(element('p', 'hint', 'No options selected. This section adds 0.'));
    }
    if (count) count.textContent = items.length ? `${items.length} selected` : 'None';
    if (details && items.length) details.open = true;
  }

  function populateSectionSelect(key) {
    const select = $('[data-option-select="' + key + '"]', dialog);
    const addButton = $('[data-option-add="' + key + '"]', dialog);
    if (!select) return;
    select.textContent = '';
    const chosen = state.sections[key] || [];
    const remaining = sectionOptions(key).filter(
      (entry) => !chosen.some((item) => item.value === entry.value)
    );
    if (!remaining.length) {
      const option = element('option', '', 'All options added');
      option.value = '';
      select.append(option);
      select.disabled = true;
      if (addButton) addButton.disabled = true;
      return;
    }
    select.disabled = false;
    if (addButton) addButton.disabled = false;
    remaining.forEach((entry) => {
      const option = element('option', '', entry.label);
      option.value = entry.value;
      select.append(option);
    });
  }

  function addSectionOption(key) {
    const select = $('[data-option-select="' + key + '"]', dialog);
    const meta = sectionMeta.get(key);
    const value = select?.value;
    if (!value) return;
    const chosen = state.sections[key] || [];
    if (chosen.some((item) => item.value === value)) return;
    chosen.push({
      value,
      label: labelFor(key, value),
      weight: clampWeight(meta?.default_weight, 5),
    });
    state.sections[key] = chosen;
    renderSection(key);
    populateSectionSelect(key);
    refreshScores();
  }

  function sectionMaximum(key) {
    const meta = sectionMeta.get(key);
    const items = state.sections[key] || [];
    if (!items.length) return 0;
    if (meta?.kind === 'exclusive') {
      return Math.max(...items.map((item) => clampWeight(item.weight, 1)));
    }
    return items.reduce((total, item) => total + clampWeight(item.weight, 1), 0);
  }

  function maximumScore() {
    const resolutionWeightValue = clampWeight(
      resolutionWeight.value, 10
    );
    const sourceMaximum = state.qualities.length
      ? Math.max(...state.qualities.map((item) => clampWeight(item.weight, 1)))
      : 0;
    return sections.reduce(
      (total, section) => total + sectionMaximum(section.key),
      resolutionWeightValue + sourceMaximum
    );
  }

  function refreshScores() {
    const maximum = maximumScore();
    maxOutput.textContent = String(maximum);
    targetInput.max = String(maximum);
    if (!targetTouched) targetInput.value = String(maximum);
    const target = Math.round(Number(targetInput.value));
    const invalid = !Number.isFinite(target) || target < 20 || target > maximum || maximum < 20;
    if (Number.isFinite(target)) {
      scoreLine.textContent = `Target ${target} / Max ${maximum}`;
    } else {
      scoreLine.textContent = `Target — / Max ${maximum}`;
    }
    scoreLine.classList.toggle('is-bad', invalid);
    if (maximum < 20) {
      scoreLine.textContent += ' — maximum must be at least 20';
    }
    targetInput.setCustomValidity(invalid ? 'Target must be between 20 and the maximum score.' : '');
  }

  function show(profile) {
    editing = profile || null;
    targetTouched = Boolean(profile);
    dialogTitle.textContent = profile ? 'Edit profile' : 'New profile';
    nameInput.value = profile?.name || '';
    populateResolutions(profile?.resolution || defaultResolution());
    resolutionWeight.value = String(
      clampWeight(profile?.resolution_weight, 10)
    );

    state.qualities = profile
      ? (profile.quality_options || []).map((item) => ({
          value: item.value,
          label: item.label || labelFor('quality_options', item.value),
          weight: clampWeight(item.weight, 10),
        }))
      : defaultQualities.map((item) => ({
          value: item.value,
          label: item.label || labelFor('quality_options', item.value),
          weight: clampWeight(item.weight, 10),
        }));

    sections.forEach((section) => {
      const source = profile?.[section.key] || [];
      state.sections[section.key] = source.map((item) => ({
        value: item.value,
        label: item.label || labelFor(section.key, item.value),
        weight: clampWeight(item.weight, section.default_weight || 5),
      }));
    });

    upgrades.checked = profile ? Boolean(profile.upgrades) : true;
    if (profile) {
      targetInput.value = String(profile.target_score ?? profile.max_score ?? 20);
    }

    renderQualityList();
    populateQualitySelect();
    sections.forEach((section) => {
      renderSection(section.key);
      populateSectionSelect(section.key);
    });
    refreshScores();
    setStatus(dialogStatus, '');
    saveButton.disabled = false;
    open(dialog);
  }

  function collectValues() {
    return {
      name: nameInput.value.trim(),
      resolution: resolutionSelect.value,
      resolution_weight: clampWeight(resolutionWeight.value, 10),
      quality_options: state.qualities.map((item) => ({
        value: item.value,
        label: item.label,
        weight: clampWeight(item.weight, 1),
      })),
      video_codecs: sectionPayload('video_codecs'),
      audio_codecs: sectionPayload('audio_codecs'),
      audio_languages: sectionPayload('audio_languages'),
      audio_channels: sectionPayload('audio_channels'),
      subtitle_formats: sectionPayload('subtitle_formats'),
      subtitle_languages: sectionPayload('subtitle_languages'),
      target_score: Math.round(Number(targetInput.value)),
      upgrades: upgrades.checked,
    };
  }

  function sectionPayload(key) {
    return (state.sections[key] || []).map((item) => ({
      value: item.value,
      label: item.label,
      weight: clampWeight(item.weight, 1),
    }));
  }

  async function save() {
    const payload = collectValues();
    if (!payload.name) {
      setStatus(dialogStatus, 'Give the profile a name.', 'bad');
      nameInput.focus();
      return;
    }
    if (!payload.quality_options.length) {
      setStatus(dialogStatus, 'Select at least one quality/source option.', 'bad');
      return;
    }
    if (!payload.resolution) {
      setStatus(dialogStatus, 'Choose a resolution.', 'bad');
      return;
    }
    const maximum = maximumScore();
    if (!Number.isFinite(payload.target_score)
        || payload.target_score < 20
        || payload.target_score > maximum
        || maximum < 20) {
      setStatus(dialogStatus, `Target score must be between 20 and ${maximum}.`, 'bad');
      targetInput.focus();
      return;
    }
    setStatus(dialogStatus, 'Saving…');
    saveButton.disabled = true;
    try {
      if (editing) {
        await patch(`/api/quality-profiles/${editing.id}`, payload);
      } else {
        await post('/api/quality-profiles', payload);
      }
      window.location.reload();
    } catch (error) {
      saveButton.disabled = false;
      setStatus(dialogStatus, error.message, 'bad');
    }
  }

  $('#new-profile')?.addEventListener('click', () => show(null));
  saveButton?.addEventListener('click', save);
  qualityAdd?.addEventListener('click', addQuality);
  qualitySelect?.addEventListener('change', () => {
    qualitySelect.dataset.selected = qualitySelect.value;
  });
  resolutionWeight.addEventListener('input', refreshScores);
  resolutionSelect.addEventListener('change', refreshScores);
  targetInput.addEventListener('input', () => {
    targetTouched = true;
    refreshScores();
  });

  sections.forEach((section) => {
    $(`[data-option-add="${section.key}"]`, dialog)
      ?.addEventListener('click', () => addSectionOption(section.key));
  });

  $$('[data-edit-profile]').forEach((button) => {
    button.addEventListener('click', () => {
      const profile = profiles.find(
        (entry) => String(entry.id) === button.dataset.editProfile
      );
      if (!profile || profile.is_builtin) return;
      show(profile);
    });
  });

  $$('[data-delete-profile]').forEach((button) => {
    button.addEventListener('click', async () => {
      const profile = profiles.find(
        (entry) => String(entry.id) === button.dataset.deleteProfile
      );
      if (!profile || profile.is_builtin) return;
      if (!window.confirm(`Delete the “${profile.name}” quality profile?`)) return;
      button.disabled = true;
      try {
        await del(`/api/quality-profiles/${profile.id}`);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        setStatus($('#profile-status'), error.message, 'bad');
      }
    });
  });
})();
