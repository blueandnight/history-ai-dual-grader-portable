(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  let resetUsername = '';
  async function request(path, payload) {
    const options = payload === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    };
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作失败');
    return data;
  }
  async function refresh() {
    await window.HISTORY_GRADER_AUTH_READY;
    if (window.HISTORY_GRADER_AUTH?.role !== 'admin') return;
    try {
      const data = await request('/api/auth/users');
      $('#users-list').replaceChildren(...data.users.map(user => {
        const card = document.createElement('div');
        card.className = 'user-row';
        const info = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = user.username;
        const meta = document.createElement('p');
        meta.className = 'muted';
        meta.textContent = `${user.role === 'admin' ? '管理员' : '教师'} · ${user.enabled ? '启用' : '停用'} · 创建于 ${new Date(user.created_at * 1000).toLocaleDateString()}`;
        info.append(title, meta);
        card.append(info);
        if (user.role === 'teacher') {
          const actions = document.createElement('div');
          actions.className = 'settings-actions';
          const reset = document.createElement('button');
          reset.type = 'button'; reset.className = 'button secondary'; reset.textContent = '重置密码';
          reset.addEventListener('click', () => {
            resetUsername = user.username;
            $('#reset-teacher-name').textContent = user.username;
            $('#reset-teacher-password').value = '';
            $('#reset-teacher-error').textContent = '';
            $('#reset-teacher-dialog').showModal();
          });
          const toggle = document.createElement('button');
          toggle.type = 'button'; toggle.className = 'button secondary'; toggle.textContent = user.enabled ? '停用' : '恢复';
          toggle.addEventListener('click', async () => {
            if (user.enabled && !window.confirm(`停用 ${user.username}？其已登录会话会立即失效。`)) return;
            toggle.disabled = true;
            try {
              await request(`/api/auth/users/${user.enabled ? 'disable' : 'enable'}`, { username: user.username });
              $('#users-status').textContent = '账号状态已更新。';
              await refresh();
            } catch (error) { $('#users-status').textContent = error.message; }
            finally { toggle.disabled = false; }
          });
          actions.append(reset, toggle); card.append(actions);
        }
        return card;
      }));
    } catch (error) { $('#users-status').textContent = error.message; }
  }
  $('#create-teacher-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.submitter; button.disabled = true;
    try {
      await request('/api/auth/users/create', { username: $('#teacher-username').value.trim(), password: $('#teacher-password').value });
      event.target.reset(); $('#users-status').textContent = '教师账号已创建，请私下告知教师用户名和临时密码。';
      await refresh();
    } catch (error) { $('#users-status').textContent = error.message; }
    finally { button.disabled = false; }
  });
  $('#reset-teacher-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.submitter; button.disabled = true;
    try {
      await request('/api/auth/users/reset-password', { username: resetUsername, password: $('#reset-teacher-password').value });
      $('#reset-teacher-password').value = ''; $('#reset-teacher-dialog').close();
      $('#users-status').textContent = '密码已重置，旧密码及已有会话已失效。';
    } catch (error) { $('#reset-teacher-error').textContent = error.message; }
    finally { button.disabled = false; }
  });
  $('#cancel-teacher-reset').addEventListener('click', () => $('#reset-teacher-dialog').close());
  $('#reset-teacher-dialog').addEventListener('close', () => { $('#reset-teacher-password').value = ''; });
  $('#refresh-users').addEventListener('click', refresh);
  document.querySelectorAll('[data-app-view-target="users"]').forEach(button => button.addEventListener('click', refresh));
  if (window.location.pathname === '/users' || window.location.hash === '#view-users') refresh();
})();
