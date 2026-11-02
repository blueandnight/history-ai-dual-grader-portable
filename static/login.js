(() => {
  'use strict';

  const loginForm = document.querySelector('#workbench-login');
  const resetForm = document.querySelector('#password-reset');
  const loginMessage = document.querySelector('#auth-message');
  const loginError = document.querySelector('#auth-error');
  const resetMessage = document.querySelector('#reset-message');
  const resetError = document.querySelector('#reset-error');
  const resetIdentity = document.querySelector('#reset-identity');
  const requestCodeButton = document.querySelector('#request-reset-code');
  const resetCodeRow = document.querySelector('#reset-code-row');
  const resetPasswordRow = document.querySelector('#reset-password-row');
  const resetConfirmRow = document.querySelector('#reset-confirm-row');
  const completeButton = document.querySelector('#complete-reset');
  let challengeId = '';

  async function postJson(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok && response.status !== 202) throw new Error(data.error || `请求失败（${response.status}）`);
    return { response, data };
  }

  async function checkSession() {
    const response = await fetch('/api/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    if (data.authenticated) {
      window.location.replace('/');
      return;
    }
    loginMessage.hidden = Boolean(data.configured);
    loginMessage.textContent = data.configured
      ? ''
      : '工作台账户尚未配置；业务页面和数据接口保持关闭。';
    document.querySelector('#forgot-password').hidden = !data.password_reset_enabled;
  }

  checkSession().catch(() => {
    loginMessage.textContent = '无法检查登录状态，请稍后刷新。';
  });

  document.querySelector('#forgot-password').addEventListener('click', () => {
    resetIdentity.value = document.querySelector('#auth-username').value.trim();
    loginForm.hidden = true;
    resetForm.hidden = false;
    resetIdentity.focus();
  });

  document.querySelector('#back-to-login').addEventListener('click', () => {
    resetForm.hidden = true;
    loginForm.hidden = false;
    resetError.textContent = '';
  });

  requestCodeButton.addEventListener('click', async () => {
    resetError.textContent = '';
    requestCodeButton.disabled = true;
    try {
      const { data } = await postJson('/api/auth/password-reset/request', { identity: resetIdentity.value.trim() });
      challengeId = data.challenge_id || '';
      resetMessage.textContent = data.message || '如果账户存在，验证码已发送到绑定邮箱。';
      resetIdentity.readOnly = true;
      requestCodeButton.hidden = true;
      resetCodeRow.hidden = false;
      resetPasswordRow.hidden = false;
      resetConfirmRow.hidden = false;
      completeButton.hidden = false;
      document.querySelector('#reset-code').focus();
    } catch (error) {
      resetError.textContent = error.message || '发送失败，请稍后重试。';
      requestCodeButton.disabled = false;
    }
  });

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    loginError.textContent = '';
    const submit = loginForm.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const { response, data } = await postJson('/api/auth/login', {
        username: document.querySelector('#auth-username').value.trim(),
        password: document.querySelector('#auth-password').value,
      });
      if (!response.ok) throw new Error(data.error || `登录失败（${response.status}）`);
      document.querySelector('#auth-password').value = '';
      window.location.replace('/');
    } catch (error) {
      loginError.textContent = error.message || '登录失败';
      submit.disabled = false;
    }
  });

  resetForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    resetError.textContent = '';
    const newPassword = document.querySelector('#reset-password-value').value;
    const confirmation = document.querySelector('#reset-password-confirm').value;
    if (newPassword !== confirmation) {
      resetError.textContent = '两次输入的新密码不一致。';
      return;
    }
    completeButton.disabled = true;
    try {
      const { data } = await postJson('/api/auth/password-reset/complete', {
        challenge_id: challengeId,
        email_code: document.querySelector('#reset-code').value.trim(),
        new_password: newPassword,
      });
      resetMessage.textContent = data.message || '密码已重置，请使用新密码登录。';
      resetForm.hidden = true;
      loginForm.hidden = false;
      document.querySelector('#auth-password').value = '';
      resetIdentity.readOnly = false;
    } catch (error) {
      resetError.textContent = error.message || '密码重置失败。';
      completeButton.disabled = false;
    }
  });
})();
