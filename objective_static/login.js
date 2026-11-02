(() => {
  'use strict';

  const form = document.querySelector('#workbench-login');
  const message = document.querySelector('#auth-message');
  const errorBox = document.querySelector('#auth-error');
  const totpRow = document.querySelector('#auth-totp-row');
  const passwordInput = document.querySelector('#auth-password');
  const totpInput = document.querySelector('#auth-totp');
  const emailRow = document.querySelector('#auth-email-row');
  const emailInput = document.querySelector('#auth-email-code');
  const usernameRow = document.querySelector('#auth-username-row');
  const passwordRow = document.querySelector('#auth-password-row');
  const usernameInput = document.querySelector('#auth-username');
  const backButton = document.querySelector('#auth-back-button');
  let emailChallengeId = '';
  let awaitingEmailCode = false;
  let totpRequired = false;

  async function checkSession() {
    const response = await fetch('/api/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    if (data.authenticated) {
      window.location.replace('/');
      return;
    }
    totpRequired = Boolean(data.totp_required);
    totpRow.hidden = !totpRequired;
    message.textContent = data.configured
      ? '请输入工作台账户。登录状态在服务重启后自动失效。'
      : '工作台账户尚未配置；业务页面和数据接口保持关闭。';
  }

  checkSession().catch(() => {
    message.textContent = '无法检查登录状态，请稍后刷新。';
  });

  backButton.addEventListener('click', () => {
    emailChallengeId = '';
    awaitingEmailCode = false;
    emailInput.value = '';
    emailInput.required = false;
    emailInput.disabled = true;
    usernameInput.disabled = false;
    passwordInput.disabled = false;
    totpInput.disabled = false;
    emailRow.hidden = true;
    usernameRow.hidden = false;
    passwordRow.hidden = false;
    totpRow.hidden = !totpRequired;
    backButton.hidden = true;
    message.textContent = '请输入工作台账户。登录状态在服务重启后自动失效。';
    form.querySelector('button[type="submit"]').textContent = '登录工作台';
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errorBox.textContent = '';
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const payload = awaitingEmailCode
        ? { challenge_id: emailChallengeId, email_code: emailInput.value }
        : {
          username: document.querySelector('#auth-username').value,
          password: passwordInput.value,
          totp_code: totpInput.value,
        };
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 202 && data.mfa_challenge_required && data.method === 'email') {
        emailChallengeId = data.challenge_id;
        awaitingEmailCode = true;
        passwordInput.value = '';
        usernameInput.disabled = true;
        passwordInput.disabled = true;
        totpInput.disabled = true;
        emailInput.disabled = false;
        emailInput.required = true;
        usernameRow.hidden = true;
        passwordRow.hidden = true;
        totpRow.hidden = true;
        emailRow.hidden = false;
        backButton.hidden = false;
        message.textContent = data.message || '动态码已发送至管理员邮箱，请在5分钟内完成验证。';
        form.querySelector('button[type="submit"]').textContent = '验证并登录';
        emailInput.focus();
        submit.disabled = false;
        return;
      }
      if (!response.ok) throw new Error(data.error || `登录失败（${response.status}）`);
      passwordInput.value = '';
      totpInput.value = '';
      emailInput.value = '';
      window.location.replace('/');
    } catch (error) {
      errorBox.textContent = error.message || '登录失败';
      submit.disabled = false;
    }
  });
})();
