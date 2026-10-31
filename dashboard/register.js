const form = document.querySelector('#registerForm');
const errorBox = document.querySelector('#registerError');
const params = new URLSearchParams(location.hash.replace(/^#/, ''));
let submitting = false;
// Keep the one-time token out of the HTTP request URL and referrer history.
if (params.has('token')) history.replaceState(null, '', location.pathname);
form.addEventListener('submit', async event => {
  event.preventDefault(); errorBox.textContent = '';
  if (submitting) return;
  submitting = true;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  const data = new FormData(form);
  try {
    const response = await fetch('/api/auth/register', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ token: params.get('token') || '', display_name: data.get('display_name'), password: data.get('password') })
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 409) throw new Error('此邮箱已注册，请使用已有账号登录。');
      if (result.detail === 'Invitation is invalid or expired') throw new Error('邀请链接已使用或已过期，请联系管理员重新生成。');
      throw new Error(response.status >= 500 ? '注册暂未完成，请稍后重试。' : result.detail || '注册失败');
    }
    location.assign('./account.html');
  } catch (error) {
    errorBox.textContent = error.message;
    submitting = false; button.disabled = false;
  }
});
if (!params.has('token')) { form.querySelector('button').disabled = true; errorBox.textContent = '邀请链接缺少注册令牌。'; }
