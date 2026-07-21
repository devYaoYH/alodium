(() => {
  // Homepage loads custom.js from <head>, before <body> exists — defer until
  // the DOM is ready, and guard against the double-inclusion re-running us.
  const boot = () => {
  if (document.getElementById('alodium-bg') || document.getElementById('alodium-chatbar')) return;

  const domain = window.location.hostname.split('.').slice(1).join('.') || window.location.hostname;

  // --- rotating scenery backdrop -------------------------------------------
  // Generated scenes served from this node (/images, mounted read-only).
  // Start with the scene that fits the hour, then drift through the set.
  const scenes = [
    { src: '/images/dawn-peaks.svg', hours: [5, 10] },
    { src: '/images/misty-lake.svg', hours: [10, 15] },
    { src: '/images/golden-dunes.svg', hours: [15, 18] },
    { src: '/images/ocean-dusk.svg', hours: [18, 21] },
    { src: '/images/aurora-night.svg', hours: [21, 29] }, // wraps past midnight
  ];
  const hour = new Date().getHours();
  let sceneIndex = scenes.findIndex(({ hours: [a, b] }) => hour >= a && hour < b);
  if (sceneIndex === -1) sceneIndex = scenes.findIndex(({ hours: [a, b] }) => hour + 24 >= a && hour + 24 < b);
  if (sceneIndex === -1) sceneIndex = 0;

  const bg = document.createElement('div');
  bg.id = 'alodium-bg';
  bg.className = 'alodium-bg';
  bg.setAttribute('aria-hidden', 'true');
  const layers = [document.createElement('div'), document.createElement('div')];
  layers.forEach((layer) => {
    layer.className = 'alodium-bg__layer';
    bg.appendChild(layer);
  });
  document.body.prepend(bg);
  scenes.forEach(({ src }) => { new Image().src = src; }); // warm the cache

  let activeLayer = 0;
  const showScene = (index) => {
    const next = 1 - activeLayer;
    layers[next].style.backgroundImage = `url("${scenes[index].src}")`;
    layers[next].classList.add('is-visible');
    layers[activeLayer].classList.remove('is-visible');
    activeLayer = next;
  };
  showScene(sceneIndex);
  setInterval(() => {
    sceneIndex = (sceneIndex + 1) % scenes.length;
    showScene(sceneIndex);
  }, 4 * 60 * 1000);

  // --- floating composer, centred and lifted off the bottom ----------------
  const chatbar = document.createElement('section');
  chatbar.id = 'alodium-chatbar';
  chatbar.innerHTML = `
    <form class="alodium-chatbar__composer">
      <label class="alodium-visually-hidden" for="alodium-request">Ask your engineering org</label>
      <textarea id="alodium-request" name="prompt" rows="1" maxlength="12000" placeholder="What would you like to make happen?"></textarea>
      <label class="alodium-visually-hidden" for="alodium-model">Answering model</label>
      <select id="alodium-model" name="model">
        <option value="claude-sonnet">Sonnet · deep</option>
        <option value="claude-haiku">Haiku · quick</option>
      </select>
      <button type="submit"><span>Send</span><span class="alodium-chatbar__spinner" aria-hidden="true"></span></button>
    </form>
    <p class="alodium-chatbar__hint">Enter to send · Shift + Enter for a new line</p>`;
  document.body.appendChild(chatbar);

  const composer = chatbar.querySelector('.alodium-chatbar__composer');
  const prompt = composer?.elements.prompt;
  const modelSelect = composer?.elements.model;
  const sendButton = composer?.querySelector('button[type="submit"]');

  // The options above mirror the assistant key's model allowlist (sonnet +
  // haiku — see config/litellm.yaml and the key minted by sso-setup.sh).
  // Remember the last choice; Open WebUI otherwise defaults to whichever
  // model LiteLLM lists first.
  const MODEL_STORAGE_KEY = 'alodium-chat-model';
  const LEGACY_MODEL_STORAGE_KEY = 'geth-chat-model';
  const storedModel = localStorage.getItem(MODEL_STORAGE_KEY);
  const savedModel = storedModel ?? localStorage.getItem(LEGACY_MODEL_STORAGE_KEY);
  if (savedModel && modelSelect && [...modelSelect.options].some((o) => o.value === savedModel)) {
    modelSelect.value = savedModel;
    if (!storedModel) {
      localStorage.setItem(MODEL_STORAGE_KEY, savedModel);
      localStorage.removeItem(LEGACY_MODEL_STORAGE_KEY);
    }
  }
  modelSelect?.addEventListener('change', () => localStorage.setItem(MODEL_STORAGE_KEY, modelSelect.value));

  // Multiline input grows the bar upward (the composer is anchored by
  // `bottom`, so extra height extends toward the top) until the CSS
  // max-height cap, after which the textarea scrolls internally.
  const autosize = () => {
    if (!prompt) return;
    prompt.style.height = 'auto';
    const cap = parseFloat(getComputedStyle(prompt).maxHeight) || 144;
    prompt.style.height = `${Math.min(prompt.scrollHeight, cap)}px`;
  };
  prompt?.addEventListener('input', autosize);

  const submitPrompt = () => {
    const value = prompt?.value.trim();
    if (!value || !sendButton) return;

    sendButton.disabled = true;
    sendButton.classList.add('is-sending');
    sendButton.querySelector('span')?.replaceChildren('Opening…');
    const destination = new URL(`https://chat.${domain}/`);
    // Open WebUI's supported URL params: `q` submits this as the first
    // message and `models` picks the answering model for the new chat.
    destination.searchParams.set('q', value);
    if (modelSelect?.value) destination.searchParams.set('models', modelSelect.value);
    window.location.assign(destination);
  };
  composer?.addEventListener('submit', (event) => {
    event.preventDefault();
    submitPrompt();
  });
  prompt?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submitPrompt();
    }
  });

  // --- global composer; Home tiles; polished tab controls -------------------
  // The composer belongs to the page shell, not any one Homepage tab. Keeping
  // the body padding on at every tab avoids cards ending beneath the sheet.
  const tabIcons = {
    Home: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1Z"/></svg>',
    Workshop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m14.7 6.3 3 3M3 21l4.7-1.2L19.4 8.1a2.1 2.1 0 0 0-3-3L4.7 16.8 3 21Z"/><path d="m12 5 7 7"/></svg>',
    Operations: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="7" rx="1.5"/><rect x="3" y="14" width="18" height="7" rx="1.5"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/></svg>',
    Security: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 20 6v5c0 5.1-3.4 8.7-8 10-4.6-1.3-8-4.9-8-10V6Z"/><path d="m8.5 12 2.2 2.2 4.8-4.8"/></svg>',
  };

  const decorateTabs = () => {
    document.querySelectorAll('[role="tab"]').forEach((tab) => {
      const label = tab.textContent.trim();
      if (!tabIcons[label] || tab.querySelector('.alodium-tab-icon')) return;
      const icon = document.createElement('span');
      icon.className = 'alodium-tab-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = tabIcons[label];
      tab.prepend(icon);
    });
  };

  const syncChrome = () => {
    chatbar.hidden = false;
    document.documentElement.classList.add('alodium-chatbar-open');
    decorateTabs();

    document.querySelectorAll('.services-group').forEach((group) => {
      const heading = group.querySelector('h1, h2, h3')?.textContent.trim();
      group.classList.toggle('alodium-tile-group', heading === 'Apps');
    });

  };
  new MutationObserver(syncChrome).observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['aria-selected'],
  });
  syncChrome();

  // --- on-demand app tile: snake -------------------------------------------
  // The snake tile is rendered by Homepage from services.yaml with id=snake-tile.
  // On-demand apps warm up "invisibly": the tile looks like any other app. We do
  // NOT add a dot — we REUSE Homepage's own container-status dot (the
  // .service-container-stats button, whose inner .docker-status .rounded-full is
  // the coloured light). We repaint that one dot from the launcher's view of the
  // app — red asleep, amber warming, green live — and hijack its click so the
  // dot (and, while asleep, the whole card) drives the launcher API at
  // launch.<domain> (behind ring1) instead of opening Homepage's stats popover.
  // A hover label reveals the action word. One dot, no overlap.

  const LAUNCHER_BASE = `https://launch.${domain}`;
  const GAME_URL = `https://game.${domain}`;

  const snakeTile = () => document.getElementById('snake-tile');

  const snakeStatus = async () => {
    try {
      const r = await fetch(`${LAUNCHER_BASE}/api/status/snake`);
      if (!r.ok) return 'stopped';
      const data = await r.json();
      return data.status;
    } catch { return 'stopped'; }
  };

  const snakeLaunch = async () => {
    try {
      const r = await fetch(`${LAUNCHER_BASE}/api/launch/snake`, { method: 'POST' });
      return r.ok;
    } catch { return false; }
  };

  const snakeStop = async () => {
    try {
      const r = await fetch(`${LAUNCHER_BASE}/api/stop/snake`, { method: 'POST' });
      return r.ok;
    } catch { return false; }
  };

  let readyPoll = null;

  // Poll the launcher until snake is up (or we give up), then flip to running
  // and — if the launch came from a click — open the game in a new tab.
  const waitUntilRunning = (openWhenReady) => {
    clearInterval(readyPoll);
    let tries = 0;
    readyPoll = setInterval(async () => {
      tries += 1;
      const s = await snakeStatus();
      if (s === 'running') {
        clearInterval(readyPoll);
        readyPoll = null;
        renderSnakeTile('running');
        if (openWhenReady) window.open(GAME_URL, '_blank');
      } else if (tries > 20) { // ~30s ceiling
        clearInterval(readyPoll);
        readyPoll = null;
        renderSnakeTile(s === 'starting' ? 'starting' : 'stopped');
      }
    }, 1500);
  };

  const doLaunch = async () => {
    renderSnakeTile('starting');
    const ok = await snakeLaunch();
    if (ok) waitUntilRunning(true);
    else renderSnakeTile('stopped');
  };

  const doStop = async () => {
    renderSnakeTile('starting'); // amber while it winds down
    await snakeStop();
    setTimeout(async () => renderSnakeTile(await snakeStatus()), 1500);
  };

  // Map a launcher status to our tile state and paint the reused native dot.
  const renderSnakeTile = (status) => {
    const tile = snakeTile();
    if (!tile) return;
    const card = tile.querySelector('.service-card') || tile;
    card.classList.add('alodium-ondemand-card');

    const state = status === 'running' ? 'running'
      : status === 'starting' ? 'starting' : 'stopped';
    card.dataset.alodiumState = state;
    // State class paints the native .docker-status dot (see custom.css).
    card.classList.remove('alodium-asleep', 'alodium-starting', 'alodium-live');
    card.classList.add(state === 'running' ? 'alodium-live'
      : state === 'starting' ? 'alodium-starting' : 'alodium-asleep');

    // Hover-reveal action word, tucked left of the reused dot. Created once.
    let label = card.querySelector('.alodium-ondemand-action');
    if (!label) {
      label = document.createElement('span');
      label.className = 'alodium-ondemand-action';
      label.setAttribute('aria-hidden', 'true');
      card.appendChild(label);
    }
    label.textContent = state === 'running' ? 'Stop'
      : state === 'starting' ? 'Starting…' : 'Launch';

    // Give the reused native dot an honest tooltip for its new job.
    const dotBtn = card.querySelector('.service-container-stats');
    if (dotBtn) {
      dotBtn.setAttribute('title', state === 'running' ? 'Stop Snake'
        : state === 'starting' ? 'Snake is starting…' : 'Launch Snake');
    }

    // One delegated, capturing click handler owns the whole card: it hijacks
    // the native dot's click (else Homepage opens its stats popover) and the
    // card's dead game link while asleep. Bound once; survives Homepage's
    // re-renders of the inner dot because it lives on the stable card element.
    if (!card.dataset.alodiumBound) {
      card.dataset.alodiumBound = '1';
      card.addEventListener('click', (e) => {
        const onControl = e.target.closest('.service-container-stats, .alodium-ondemand-action');
        const st = card.dataset.alodiumState;
        if (st === 'starting') { if (onControl) { e.preventDefault(); e.stopPropagation(); } return; }
        if (onControl) {
          e.preventDefault();
          e.stopPropagation();
          if (st === 'running') doStop(); else doLaunch();
          return;
        }
        // A click anywhere else on the card: while asleep, launch (the href
        // points at a game that isn't up yet); once live, let it open the game.
        if (st !== 'running') { e.preventDefault(); e.stopPropagation(); doLaunch(); }
      }, true);
    }
  };

  const initSnakeTile = async () => {
    const tile = snakeTile();
    if (!tile) return;
    renderSnakeTile(await snakeStatus());
    // Refresh in the background, but never stomp on an in-flight launch/stop.
    setInterval(async () => {
      if (readyPoll) return;
      if (snakeTile()?.querySelector('.service-card')?.dataset.alodiumState === 'starting') return;
      renderSnakeTile(await snakeStatus());
    }, 30000);
  };

  // Wait for the tile to appear (Homepage renders async)
  const observer = new MutationObserver(() => {
    if (snakeTile()) {
      observer.disconnect();
      initSnakeTile();
    }
  });
  observer.observe(document.body, { subtree: true, childList: true });
  // Also try immediately in case it's rendered already
  initSnakeTile();

  // Homepage only honours the hash on a fresh load, and renders every card
  // link with target="_blank". Internal hash links (e.g. the Home "Waiting on
  // you" card → /#workshop) should switch tabs in place instead: intercept the
  // click before React's anchor handles it and drive the tab directly.
  const switchToHashTab = (hash) => {
    const name = hash.replace(/^#/, '').toLowerCase();
    const tab = [...document.querySelectorAll('[role="tab"]')]
      .find((t) => t.textContent.trim().toLowerCase() === name);
    tab?.click();
    return !!tab;
  };
  document.addEventListener('click', (event) => {
    const link = event.target.closest?.('a[href^="/#"]');
    if (!link) return;
    const hash = link.getAttribute('href').slice(1);
    if (switchToHashTab(hash)) {
      event.preventDefault();
      event.stopPropagation();
      history.replaceState(null, '', hash);
    }
  }, true);
  window.addEventListener('hashchange', () => switchToHashTab(window.location.hash));

  // --- deploy-info: last-deployed timestamp + commit link in the lower-left ---
  // Fetches /static/deploy-info.json (written by deploy.sh after each successful
  // deploy) and renders a small indicator showing the deployed commit hash
  // (hyperlinked to Forgejo) and the deploy timestamp. Silently absent if the
  // artifact doesn't exist yet (first install / pre-deploy).
  const DEPLOY_INFO_URL = '/static/deploy-info.json';
  const DEPLOY_STORAGE_KEY = 'alodium-deploy-info';

  const renderDeployInfo = (info) => {
    // Remove any stale widget first
    const existing = document.getElementById('alodium-deploy-info');
    if (existing) existing.remove();

    if (!info || !info.commit) return;

    const el = document.createElement('div');
    el.id = 'alodium-deploy-info';
    el.className = 'alodium-deploy-info';

    const ts = info.timestamp
      ? new Date(info.timestamp).toLocaleString(undefined, {
          year: 'numeric', month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit',
        })
      : 'unknown';

    el.innerHTML = `
      <span class="alodium-deploy-info__label">deployed</span>
      <a class="alodium-deploy-info__commit" href="${info.url || '#'}"
         target="_blank" rel="noopener" title="${info.commit || ''}">
        ${info.short_hash || info.commit?.slice(0, 7) || '?'}
      </a>
      <span class="alodium-deploy-info__ts">${ts}</span>
    `;
    document.body.appendChild(el);
  };

  const fetchDeployInfo = async () => {
    try {
      const r = await fetch(DEPLOY_INFO_URL, { cache: 'no-cache' });
      if (!r.ok) return;
      const info = await r.json();
      renderDeployInfo(info);
    } catch {
      // Not yet deployed — that's fine, stay silent.
    }
  };

  // Fetch immediately and re-check every 2 minutes (catches the first deploy
  // on a fresh install / after the artifact is created by deploy-watch).
  fetchDeployInfo();
  setInterval(fetchDeployInfo, 120000);
  };

  if (document.body) boot();
  else document.addEventListener('DOMContentLoaded', boot, { once: true });
})();
