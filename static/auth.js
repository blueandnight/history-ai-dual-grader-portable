(() => {
  'use strict';

  const nativeFetch = window.fetch.bind(window);
  const authState = { authenticated: false, role: '', csrfToken: '', required: true, configured: false, cloudMode: false, totpRequired: false, emailOtpRequired: false, mobileTokenOnly: false };
  window.HISTORY_GRADER_AUTH = authState;

  const mobileTokenParam = new URLSearchParams(window.location.search).get('access_token');
  if (mobileTokenParam) {
    try { sessionStorage.setItem('history-grader-mobile-token', mobileTokenParam); } catch (error) { /* private mode */ }
  }
  function storedMobileToken() {
    try { return sessionStorage.getItem('history-grader-mobile-token') || ''; } catch (error) { return ''; }
  }

  const gate = document.createElement('div');
  gate.className = 'auth-gate';
  gate.innerHTML = `
    <form class="auth-card" id="workbench-login" autocomplete="on">
      <span class="eyebrow">私人试用 · 安全登录</span>
      <h2>阅卷工作台登录</h2>
      <p id="auth-message">使用邮箱或用户名和密码登录；可信会话最长保持30天。</p>
      <label id="auth-username-row">邮箱或用户名<input id="auth-username" name="username" autocomplete="username" maxlength="254" required></label>
      <label id="auth-password-row">密码<input id="auth-password" name="password" type="password" autocomplete="current-password" maxlength="512" required></label>
      <label id="auth-totp-row" hidden>双因素验证码<input id="auth-totp" name="totp_code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6"></label>
      <label id="auth-email-row" hidden>邮箱动态码<input id="auth-email-code" name="email_code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" disabled></label>
      <button class="button primary" type="submit">登录工作台</button>
      <button class="button" id="auth-back-button" type="button" hidden>返回账户输入</button>
      <p class="auth-error" id="auth-error" role="alert" aria-live="polite"></p>
    </form>`;
  document.body.appendChild(gate);
  document.body.classList.add('auth-locked');

  function showGate(message = '') {
    gate.hidden = false;
    document.body.classList.add('auth-locked');
    if (message) document.querySelector('#auth-error').textContent = message;
  }

  function hideGate() {
    gate.hidden = true;
    document.body.classList.remove('auth-locked');
  }

  function addAuthenticatedHeader() {
    if (!authState.authenticated || document.querySelector('.auth-user')) return;
    const host = document.querySelector('.header-actions');
    if (!host) return;
    const row = document.createElement('div');
    row.className = 'auth-user';
    row.innerHTML = '<span>账户已登录</span><button class="auth-logout" type="button">退出</button>';
    row.querySelector('button').addEventListener('click', async () => {
      try {
        await nativeFetch('/api/auth/logout', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': authState.csrfToken },
          body: '{}',
        });
      } finally {
        window.location.reload();
      }
    });
    host.prepend(row);
  }

  function adaptWebsiteMode() {
    document.body.classList.toggle('auth-teacher', authState.role === 'teacher');
    document.body.classList.toggle('auth-admin', authState.role === 'admin');
    document.querySelectorAll('[data-app-view-target="users"]').forEach(el => { el.hidden = authState.role !== 'admin'; });
    if (authState.role === 'teacher') {
      const roster = document.querySelector('#roster-section');
      const grading = document.querySelector('#view-grading');
      if (roster && grading) grading.appendChild(roster);
      document.querySelectorAll('[data-app-view-target="management"], .service-menu').forEach(el => { el.hidden = true; });
      document.querySelectorAll('a[href="#model-settings"], a[href="#mobile-access-panel"]').forEach(el => {
        if (el.classList.contains('readiness-card')) {
          el.removeAttribute('href');
        } else { el.hidden = true; }
      });
      if (['management', 'users'].includes(document.body.dataset.appView)) document.querySelector('#main-tab-grading')?.click();
      return;
    }
    const managementDescription = document.querySelector('#view-management .page-heading p');
    if (managementDescription) managementDescription.textContent = '完整管理员会话在手机和电脑拥有相同业务权限；页面布局随屏幕调整。';
    const tokenPanel = document.querySelector('#mobile-access-panel .token-panel');
    if (tokenPanel) {
      const note = tokenPanel.querySelector('#mobile-token-note');
      if (note) note.textContent = '可生成45分钟临时码用于上传答卷、查看手机提交记录的证据并确认成绩；不可读取花名册或使用服务器管理功能。';
    }
    const mobileDescription = document.querySelector('#mobile-access-panel .section-heading p');
    if (mobileDescription) mobileDescription.textContent = '手机可使用同一管理员账户登录，也可生成限时访问码批改手机提交的答卷；临时码不具备管理员权限。';
    const connectionSteps = document.querySelector('#mobile-access-panel .connection-steps');
    if (connectionSteps) connectionSteps.hidden = true;
    const networkInfo = document.querySelector('#mobile-access-panel .mobile-network-info');
    if (networkInfo) networkInfo.hidden = true;
    const settingsForm = document.querySelector('#settings-form');
    if (settingsForm) {
      const keyInput = settingsForm.querySelector('#api-key');
      if (keyInput) { keyInput.value = ''; keyInput.disabled = true; }
      settingsForm.querySelectorAll('input[name="key-storage"]').forEach((control) => { control.disabled = true; });
      const keyOptions = settingsForm.querySelector('.key-storage-options');
      if (keyOptions) keyOptions.hidden = true;
      const baseUrl = settingsForm.querySelector('#model-base-url');
      const modelId = settingsForm.querySelector('#model-id');
      if (baseUrl) baseUrl.readOnly = true;
      if (modelId) modelId.readOnly = true;
      const customPresets = settingsForm.querySelector('.advanced-settings');
      if (customPresets) customPresets.hidden = true;
      const note = settingsForm.querySelector('#settings-note');
      if (note) note.textContent = 'API Key 由服务器安全保存，不会发送到浏览器。';
      const description = document.querySelector('#model-settings .section-heading p');
      if (description) description.textContent = '管理员可选择服务器已配置的模型；API Key 保存在服务器，不会发送到手机或浏览器。';
      const kicker = document.querySelector('#model-settings .section-kicker');
      if (kicker) kicker.textContent = '服务器模型配置';
      const badge = document.querySelector('#model-settings .privacy-badge');
      if (badge) badge.textContent = '仅完整管理员会话';
    }
  }

  async function readSession() {
    const response = await nativeFetch('/api/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    authState.required = data.required !== false;
    authState.configured = Boolean(data.configured);
    authState.authenticated = Boolean(data.authenticated);
    authState.role = data.role || '';
    authState.cloudMode = Boolean(data.cloud_mode);
    authState.totpRequired = Boolean(data.totp_required);
    authState.emailOtpRequired = Boolean(data.email_otp_required);
    authState.csrfToken = data.csrf_token || '';
    document.querySelector('#auth-totp-row').hidden = !authState.totpRequired;
    window.HISTORY_GRADER_AUTH = authState;
    if (!authState.required) {
      hideGate();
      return true;
    }
    if (authState.authenticated) {
      hideGate();
      addAuthenticatedHeader();
      adaptWebsiteMode();
      return true;
    }
    const mobileToken = storedMobileToken();
    if (mobileToken) {
      const probe = await nativeFetch('/api/assignments', {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Authorization: `Bearer ${mobileToken}` },
      }).catch(() => null);
      if (probe?.ok) {
        authState.mobileTokenOnly = true;
        authState.cloudMode = true;
        window.HISTORY_GRADER_AUTH = authState;
        document.body.classList.add('mobile-token-only');
        hideGate();
        return true;
      }
    }
    window.location.replace('/login');
    return false;
  }

  const authReady = readSession().catch(() => {
    showGate('无法检查登录状态，请稍后刷新。');
    return false;
  });
  window.HISTORY_GRADER_AUTH_READY = authReady;

  let emailChallengeId = '';
  let awaitingEmailCode = false;

  window.fetch = async (input, options = {}) => {
    const rawUrl = typeof input === 'string' ? input : input.url;
    const url = new URL(rawUrl, window.location.href);
    const protectedApi = url.origin === window.location.origin
      && url.pathname.startsWith('/api/')
      && !['/api/health', '/api/auth/session', '/api/auth/login'].includes(url.pathname);
    if (protectedApi) await authReady;
    const method = String(options.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase();
    const headers = new Headers(options.headers || (typeof input === 'string' ? undefined : input.headers));
    if (protectedApi && !['GET', 'HEAD', 'OPTIONS'].includes(method) && authState.csrfToken) {
      headers.set('X-CSRF-Token', authState.csrfToken);
    }
    const response = await nativeFetch(input, { ...options, headers, credentials: 'same-origin' });
    if (protectedApi && response.status === 401 && !authState.mobileTokenOnly) window.location.replace('/login');
    return response;
  };

  document.querySelector('#auth-back-button').addEventListener('click', () => {
    emailChallengeId = '';
    awaitingEmailCode = false;
    const emailInput = document.querySelector('#auth-email-code');
    emailInput.value = '';
    emailInput.required = false;
    emailInput.disabled = true;
    document.querySelector('#auth-username').disabled = false;
    document.querySelector('#auth-password').disabled = false;
    document.querySelector('#auth-totp').disabled = false;
    document.querySelector('#auth-email-row').hidden = true;
    document.querySelector('#auth-username-row').hidden = false;
    document.querySelector('#auth-password-row').hidden = false;
    document.querySelector('#auth-totp-row').hidden = !authState.totpRequired;
    document.querySelector('#auth-back-button').hidden = true;
    document.querySelector('#auth-message').textContent = '使用邮箱或用户名和密码登录；可信会话最长保持30天。';
    document.querySelector('#workbench-login button[type="submit"]').textContent = '登录工作台';
  });

  document.querySelector('#workbench-login').addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorBox = document.querySelector('#auth-error');
    errorBox.textContent = '';
    const submit = event.currentTarget.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const payload = awaitingEmailCode
        ? { challenge_id: emailChallengeId, email_code: document.querySelector('#auth-email-code').value }
        : {
          username: document.querySelector('#auth-username').value,
          password: document.querySelector('#auth-password').value,
          totp_code: document.querySelector('#auth-totp').value,
        };
      const response = await nativeFetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 202 && data.mfa_challenge_required && data.method === 'email') {
        emailChallengeId = data.challenge_id;
        awaitingEmailCode = true;
        document.querySelector('#auth-password').value = '';
        document.querySelector('#auth-username').disabled = true;
        document.querySelector('#auth-password').disabled = true;
        document.querySelector('#auth-totp').disabled = true;
        document.querySelector('#auth-email-code').disabled = false;
        document.querySelector('#auth-email-code').required = true;
        document.querySelector('#auth-username-row').hidden = true;
        document.querySelector('#auth-password-row').hidden = true;
        document.querySelector('#auth-totp-row').hidden = true;
        document.querySelector('#auth-email-row').hidden = false;
        document.querySelector('#auth-back-button').hidden = false;
        document.querySelector('#auth-message').textContent = data.message || '动态码已发送至管理员邮箱，请在5分钟内完成验证。';
        document.querySelector('#workbench-login button[type="submit"]').textContent = '验证并登录';
        document.querySelector('#auth-email-code').focus();
        submit.disabled = false;
        return;
      }
      if (!response.ok) throw new Error(data.error || `登录失败（${response.status}）`);
      document.querySelector('#auth-password').value = '';
      document.querySelector('#auth-totp').value = '';
      document.querySelector('#auth-email-code').value = '';
      window.location.reload();
    } catch (error) {
      errorBox.textContent = error.message || '登录失败';
      submit.disabled = false;
    }
  });
})();
