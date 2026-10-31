let csrf = '';
let busy = false;
const $ = selector => document.querySelector(selector);
const states = { pending: '等待创建', starting: '启动中', ready: '运行中', stopping: '停止中', stopped: '已停止', suspended: '账号已停用', failed: '操作失败' };

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...options.headers };
  if (options.method) headers['X-CSRF-Token'] = csrf;
  const response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) { location.assign('./login.html'); throw new Error('请重新登录'); }
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

async function loadInstance() {
  try {
    const { instance, enabled } = await api('/api/instance');
    $('#createInstance').hidden = !!instance || !enabled;
    $('#startInstance').hidden = !instance || !['stopped', 'failed'].includes(instance.status);
    $('#stopInstance').hidden = !instance || ['stopped', 'suspended', 'stopping'].includes(instance.status);
    $('#openInstance').hidden = !instance || instance.status !== 'ready' || !instance.worker_online;
    $('#instanceStatus').textContent = instance ? states[instance.status] || instance.status : '尚未创建';
    $('#instanceDetail').textContent = !enabled ? '实例管理正在配置中。' : !instance ? '创建后拥有独立容器、2 核 CPU、2 GiB 内存及 8 GiB 持久存储。' : `2 核 CPU · 2 GiB 内存 · 8 GiB 存储${instance.version ? ` · ${instance.version}` : ''}${instance.worker_online ? '' : ' · 管理进程暂未响应'}`;
    $('#instanceError').textContent = instance?.error_code ? `操作未完成（${instance.error_code}），可重试启动或联系管理员。` : '';
  } catch (error) { $('#instanceError').textContent = error.message; }
}

async function act(path) {
  if (busy) return;
  busy = true;
  try { await api(path, { method: 'POST' }); await loadInstance(); }
  catch (error) { $('#instanceError').textContent = error.message; }
  finally { busy = false; }
}
$('#createInstance').addEventListener('click', () => act('/api/instance'));
$('#startInstance').addEventListener('click', () => act('/api/instance/start'));
$('#stopInstance').addEventListener('click', () => act('/api/instance/stop'));
$('#refreshInstance').addEventListener('click', loadInstance);
$('#openInstance').addEventListener('click', async () => {
  if (busy) return;
  busy = true; $('#openInstance').disabled = true;
  try {
    const ticket = await api('/api/instance/launch', { method: 'POST' });
    for (let attempt = 0; attempt < 25; attempt++) {
      const result = await api(`/api/instance/launch/${encodeURIComponent(ticket.request_id)}/consume`, { method: 'POST' });
      if (result.url) {
        // Never accept another origin/path even if a server response is malformed.
        const target = new URL(result.url);
        if (target.protocol !== 'https:' || !/^u-[a-f0-9]{32}\.botawaken\.com$/.test(target.hostname) || target.port || target.pathname !== '/' || target.username || target.password || target.hash) throw new Error('实例地址无效');
        location.assign(target.href); return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    throw new Error('获取入口超时，请稍后重试');
  } catch (error) { $('#instanceError').textContent = error.message; }
  finally { busy = false; $('#openInstance').disabled = false; }
});
$('#logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); location.assign('./login.html'); }
  catch (error) { $('#instanceError').textContent = error.message; }
});
(async () => {
  try {
    const session = await api('/api/auth/session'); csrf = session.csrf_token;
    $('#userName').textContent = session.user.display_name; $('#userEmail').textContent = session.user.email;
    await loadInstance();
    setInterval(() => { if (!busy && !document.hidden) loadInstance(); }, 5000);
  } catch (error) { $('#instanceError').textContent = error.message; }
})();
