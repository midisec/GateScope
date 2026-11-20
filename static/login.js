(() => {
  'use strict';
  const form = document.getElementById('login-form');
  const error = document.getElementById('error');
  const submit = document.getElementById('submit');

  form.addEventListener('submit', async event => {
    event.preventDefault();
    error.textContent = '';
    submit.disabled = true;
    try {
      const response = await fetch('/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('username').value,
          password: document.getElementById('password').value,
        }),
      });
      if (!response.ok) throw new Error('账号或密码错误，请稍后重试');
      window.location.replace('/');
    } catch (reason) {
      error.textContent = reason.message || '登录失败';
    } finally {
      submit.disabled = false;
    }
  });
})();

