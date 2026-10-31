document.querySelector('#loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const errorBox = document.querySelector('#loginError'); errorBox.textContent = '';
  const form = new FormData(event.currentTarget);
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ email: form.get('email'), password: form.get('password') })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '登录失败');
    location.assign(result.user.role === 'admin' ? './console.html' : './account.html');
  } catch (error) { errorBox.textContent = error.message; }
});
