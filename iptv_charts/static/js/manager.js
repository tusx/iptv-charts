/* Shared helpers for the Movies, Series, Activity and Scan pages.
 *
 * The app deliberately has no build step and no framework; this file keeps the
 * fetch, dialog and status conventions in one place so the new pages behave
 * like the existing ones. */

(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  async function request(url, options = {}) {
    const response = await fetch(url, options);
    let payload = {};
    const type = response.headers.get('content-type') || '';
    if (type.includes('application/json')) {
      payload = await response.json().catch(() => ({}));
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Request failed (HTTP ${response.status})`);
    }
    return payload;
  }

  function jsonRequest(method, url, body) {
    const options = {
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (body !== undefined) options.body = JSON.stringify(body);
    return request(url, options);
  }

  const get = (url) => request(url);
  const post = (url, body = {}) => jsonRequest('POST', url, body);
  const patch = (url, body = {}) => jsonRequest('PATCH', url, body);
  const del = (url, body = {}) => jsonRequest('DELETE', url, body);

  function setStatus(element, message, kind = '') {
    if (!element) return;
    element.textContent = message || '';
    element.classList.toggle('is-bad', kind === 'bad');
    element.classList.toggle('is-ok', kind === 'ok');
  }

  function feedback(button, message, ok = true) {
    if (!button) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = ok ? `✓ ${message}` : '✕ Failed';
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = original;
    }, ok ? 1500 : 2200);
  }

  function debounce(fn, wait = 300) {
    let timer = null;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), wait);
    };
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function formatBytes(value) {
    const number = Number(value) || 0;
    if (number <= 0) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    let index = 0;
    let size = number;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${size.toFixed(size >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function timeAgo(timestamp) {
    if (!timestamp) return 'never';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000) - Number(timestamp));
    if (seconds < 90) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hour${Math.floor(seconds / 3600) === 1 ? '' : 's'} ago`;
    if (seconds < 2592000) return `${Math.floor(seconds / 86400)} day${Math.floor(seconds / 86400) === 1 ? '' : 's'} ago`;
    if (seconds < 31536000) return `${Math.floor(seconds / 2592000)} month${Math.floor(seconds / 2592000) === 1 ? '' : 's'} ago`;
    return `${Math.floor(seconds / 31536000)} year${Math.floor(seconds / 31536000) === 1 ? '' : 's'} ago`;
  }

  function open(dialog) {
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    const focusable = dialog.querySelector('input, select, button');
    if (focusable) window.setTimeout(() => focusable.focus(), 20);
  }

  function close(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }

  function bindDialogs() {
    $$('dialog').forEach((dialog) => {
      dialog.addEventListener('click', (event) => {
        if (event.target === dialog) close(dialog);
      });
    });
    $$('[data-close]').forEach((button) => {
      button.addEventListener('click', () => close(button.closest('dialog')));
    });
  }

  function imageUrl(size, path) {
    if (!path) return null;
    return `/api/images/tmdb/${size}/${String(path).replace(/^\/+/, '')}`;
  }

  bindDialogs();

  window.Manager = {
    $, $$, request, get, post, patch, del,
    setStatus, feedback, debounce, element, formatBytes, timeAgo,
    open, close, imageUrl,
  };
})();
