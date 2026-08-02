(() => {
  'use strict';

  const pathParts = location.pathname.split('/').filter(Boolean);
  const invite = pathParts[0] === 'layover' ? pathParts[1] : '';
  const apiRoot = `/api/layover-web/invites/${encodeURIComponent(invite)}`;
  const storageKey = `aerox-layover-${invite.slice(0, 20)}`;
  const $ = (id) => document.getElementById(id);
  const views = ['loading-view', 'error-view', 'join-view', 'chat-view'];
  const state = {
    session: null,
    profile: null,
    selectedAvatar: '✈️',
    messages: new Map(),
    pollTimer: null,
    deferredInstall: null,
    sending: false,
  };

  const show = (id) => views.forEach((view) => $(view).classList.toggle('hidden', view !== id));
  const setBusy = (button, busy, busyText, normalText) => {
    button.disabled = busy;
    button.textContent = busy ? busyText : normalText;
  };

  function restoreSession() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (value && value.session && value.profile) {
        state.session = value.session;
        state.profile = value.profile;
      }
    } catch (_) {}
  }

  function persistSession() {
    localStorage.setItem(storageKey, JSON.stringify({session: state.session, profile: state.profile}));
  }

  function clearSession() {
    state.session = null;
    state.profile = null;
    localStorage.removeItem(storageKey);
  }

  async function api(path = '', options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body) headers.set('Content-Type', 'application/json');
    if (state.session) headers.set('Authorization', `Bearer ${state.session}`);
    const response = await fetch(`${apiRoot}${path}`, {...options, headers, cache: 'no-store'});
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) {
      const error = new Error(payload.error || `http_${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function friendlyError(error) {
    const code = error && error.message;
    if (code === 'rate_limited') return 'Zu viele Versuche. Warte bitte kurz.';
    if (code === 'invite_full') return 'Dieser Gastzugang ist bereits voll.';
    if (code === 'storage_unavailable' || code === 'storage_unavailable_or_full') return 'Der Chat ist kurz nicht erreichbar. Versuch es gleich noch einmal.';
    if (!navigator.onLine || error instanceof TypeError) return 'Keine Verbindung. Prüf dein Internet und versuch es erneut.';
    return 'Das hat nicht geklappt. Versuch es bitte noch einmal.';
  }

  function renderAvatars(avatars) {
    const grid = $('avatar-grid');
    grid.replaceChildren();
    (avatars || []).forEach((avatar, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `avatar-choice${avatar === state.selectedAvatar ? ' selected' : ''}`;
      button.textContent = avatar;
      button.setAttribute('aria-label', `Avatar ${index + 1}`);
      button.setAttribute('aria-pressed', avatar === state.selectedAvatar ? 'true' : 'false');
      button.addEventListener('click', () => {
        state.selectedAvatar = avatar;
        renderAvatars(avatars);
      });
      grid.append(button);
    });
  }

  async function boot() {
    show('loading-view');
    if (!invite) return showFatal('Ungültiger Einladungslink.');
    try {
      const meta = await api();
      $('group-name').textContent = meta.group_name || 'Layover Chat';
      document.title = `${meta.group_name || 'Layover Chat'} · AeroX`;
      renderAvatars(meta.avatars);
      restoreSession();
      setupNativeLink();
      if (state.session) {
        await enterChat();
      } else {
        show('join-view');
        setTimeout(() => $('guest-name').focus(), 80);
      }
    } catch (error) {
      showFatal(error.status === 404
        ? 'Die Einladung ist abgelaufen oder wurde deaktiviert.'
        : friendlyError(error));
    }
  }

  function showFatal(text) {
    $('error-text').textContent = text;
    show('error-view');
  }

  function setupNativeLink() {
    const legacyCode = new URLSearchParams(location.search).get('c');
    if (!legacyCode || !/^[A-Za-z0-9_-]+$/.test(legacyCode)) return;
    const link = $('native-link');
    link.href = `aerox-lg:${legacyCode}`;
    link.classList.remove('hidden');
  }

  async function join(event) {
    event.preventDefault();
    const name = $('guest-name').value.trim().replace(/\s+/g, ' ');
    if (!name) return;
    const errorNode = $('join-error');
    errorNode.classList.add('hidden');
    setBusy($('join-button'), true, 'Wird verbunden…', 'Chat beitreten');
    try {
      const result = await api('/join', {
        method: 'POST',
        body: JSON.stringify({name, avatar: state.selectedAvatar}),
      });
      state.session = result.session_token;
      state.profile = {name: result.display_name, avatar: result.avatar};
      persistSession();
      await enterChat();
    } catch (error) {
      errorNode.textContent = friendlyError(error);
      errorNode.classList.remove('hidden');
    } finally {
      setBusy($('join-button'), false, 'Wird verbunden…', 'Chat beitreten');
    }
  }

  async function enterChat() {
    show('chat-view');
    state.messages.clear();
    try {
      await loadMessages(false);
      schedulePoll();
      requestAnimationFrame(() => scrollToBottom(false));
    } catch (error) {
      if (error.status === 401) {
        clearSession();
        show('join-view');
      } else {
        setConnection(false);
        schedulePoll(5000);
      }
    }
  }

  function renderMessages() {
    const list = $('message-list');
    const sorted = [...state.messages.values()].sort((a, b) => (a.ts || 0) - (b.ts || 0));
    list.replaceChildren();
    $('empty-chat').classList.toggle('hidden', sorted.length !== 0);
    sorted.forEach((message) => {
      const item = document.createElement('li');
      item.className = `message${message.is_mine ? ' mine' : ''}`;

      const author = document.createElement('p');
      author.className = 'message-author';
      author.textContent = message.is_mine ? `${state.profile?.avatar || ''} Du` : (message.author_name || 'Crew');

      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.textContent = message.text || '';

      const time = document.createElement('p');
      time.className = 'message-time';
      time.textContent = message.ts ? new Intl.DateTimeFormat('de-DE', {hour: '2-digit', minute: '2-digit'}).format(new Date(message.ts * 1000)) : '';

      item.append(author, bubble, time);
      list.append(item);
    });
  }

  async function loadMessages(incremental = true) {
    const values = [...state.messages.values()];
    const latest = incremental && values.length ? Math.max(...values.map((m) => m.ts || 0)) : 0;
    const query = latest ? `?since_ts=${encodeURIComponent(latest)}` : '';
    const result = await api(`/messages${query}`);
    let added = false;
    (result.messages || []).forEach((message) => {
      if (!state.messages.has(message.id)) added = true;
      state.messages.set(message.id, message);
    });
    if (added || !incremental) {
      const nearBottom = document.documentElement.scrollHeight - innerHeight - scrollY < 180;
      renderMessages();
      if (nearBottom || !incremental) requestAnimationFrame(() => scrollToBottom(added));
    }
    setConnection(true);
  }

  function schedulePoll(delay = 3500) {
    clearTimeout(state.pollTimer);
    if (document.hidden) return;
    state.pollTimer = setTimeout(async () => {
      try { await loadMessages(true); } catch (error) {
        if (error.status === 401) {
          clearSession();
          show('join-view');
          return;
        }
        setConnection(false);
      }
      schedulePoll();
    }, delay);
  }

  function setConnection(connected) {
    $('connection-pill').classList.toggle('hidden', connected);
  }

  function scrollToBottom(smooth) {
    window.scrollTo({top: document.documentElement.scrollHeight, behavior: smooth ? 'smooth' : 'auto'});
  }

  async function send(event) {
    event.preventDefault();
    if (state.sending) return;
    const input = $('message-input');
    const text = input.value.trim();
    if (!text) return;
    state.sending = true;
    $('send-button').disabled = true;
    try {
      const result = await api('/messages', {method: 'POST', body: JSON.stringify({text})});
      state.messages.set(result.message.id, result.message);
      input.value = '';
      resizeComposer();
      renderMessages();
      requestAnimationFrame(() => scrollToBottom(true));
      setConnection(true);
    } catch (error) {
      setConnection(false);
      window.alert(friendlyError(error));
    } finally {
      state.sending = false;
      $('send-button').disabled = false;
      input.focus();
    }
  }

  function resizeComposer() {
    const input = $('message-input');
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }

  function setupInstall() {
    window.addEventListener('beforeinstallprompt', (event) => {
      event.preventDefault();
      state.deferredInstall = event;
    });
    $('install-button').addEventListener('click', async () => {
      if (state.deferredInstall) {
        state.deferredInstall.prompt();
        await state.deferredInstall.userChoice;
        state.deferredInstall = null;
        return;
      }
      const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
      $('install-help').textContent = ios
        ? 'In Safari auf Teilen tippen und „Zum Home-Bildschirm“ wählen. Danach startet der Chat wie eine App.'
        : 'Im Browser-Menü „App installieren“ oder „Zum Startbildschirm hinzufügen“ wählen.';
      $('install-dialog').showModal();
    });
  }

  $('join-form').addEventListener('submit', join);
  $('message-form').addEventListener('submit', send);
  $('message-input').addEventListener('input', resizeComposer);
  $('message-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('message-form').requestSubmit();
    }
  });
  $('retry-button').addEventListener('click', boot);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.session) {
      loadMessages(true).catch(() => setConnection(false));
      schedulePoll();
    } else {
      clearTimeout(state.pollTimer);
    }
  });
  window.addEventListener('online', () => state.session && loadMessages(true).catch(() => {}));

  setupInstall();
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/layover-sw.js', {scope: '/layover/'}).catch(() => {});
  }
  boot();
})();
