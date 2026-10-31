let csrfToken = '';
let currentUser = null;
const $ = (selector, root = document) => root.querySelector(selector);
const loginView = $('#loginView');
const appView = $('#appView');
const loginError = $('#loginError');

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('Accept', 'application/json');
  if (options.body) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET' && csrfToken) headers.set('X-CSRF-Token', csrfToken);
  const method = (options.method || 'GET').toUpperCase();
  const attempts = method === 'GET' ? 3 : 1;
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.detail || `请求失败 (${response.status})`);
      return payload;
    } catch (error) {
      lastError = error.name === 'AbortError' ? new Error('网络响应超时，后台任务仍会继续') : error;
      if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt));
    } finally { clearTimeout(timer); }
  }
  throw lastError;
}

function showApp(user) {
  currentUser = user;
  loginView.hidden = true;
  appView.hidden = false;
  $('#logout').hidden = false;
  $('#welcome').textContent = `${user.display_name} · ${user.email} · 管理员`;
  loadAll();
}

function showLogin(message = '') {
  currentUser = null;
  csrfToken = '';
  appView.hidden = true;
  loginView.hidden = false;
  $('#logout').hidden = true;
  loginError.textContent = message;
}

async function refreshSession() {
  try {
    const session = await api('/api/auth/session');
    csrfToken = session.csrf_token;
    if (session.user.role !== 'admin') return showLogin('此页面仅供管理员使用。');
    showApp(session.user);
  } catch {
    showLogin();
  }
}

function makeCell(text, className = '') {
  const cell = document.createElement('td');
  if (className) cell.className = className;
  cell.textContent = text ?? '';
  return cell;
}

function serverDate(value) {
  if (!value) return null;
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

async function loadUsers() {
  const body = $('#usersBody');
  const error = $('#usersError');
  error.textContent = '';
  try {
    const { users } = await api('/api/admin/users');
    body.replaceChildren();
    $('#userCount').textContent = users.length;
    for (const user of users) {
      const row = document.createElement('tr');
      const identity = document.createElement('td');
      identity.className = 'email-cell';
      const name = document.createElement('b'); name.textContent = user.display_name;
      const email = document.createElement('small'); email.textContent = user.email;
      identity.append(name, email);
      row.append(identity);
      const role = makeCell(user.role === 'admin' ? '管理员' : '用户');
      const rolePill = document.createElement('span'); rolePill.className = `role-pill ${user.role}`; rolePill.textContent = role.textContent; role.replaceChildren(rolePill); row.append(role);
      const state = makeCell('');
      const pill = document.createElement('span'); pill.className = `state-pill ${user.status}`; pill.textContent = user.status === 'active' ? '正常' : '已停用'; state.append(pill); row.append(state);
      row.append(makeCell(serverDate(user.created_at)?.toLocaleDateString('zh-CN') || '—'));
      const action = makeCell('');
      const button = document.createElement('button');
      button.className = 'row-action';
      button.textContent = user.status === 'active' ? '停用账号' : '恢复账号';
      button.disabled = user.id === currentUser.id;
      button.addEventListener('click', () => changeStatus(user.id, user.status === 'active' ? 'suspended' : 'active'));
      action.append(button); row.append(action);
      body.append(row);
    }
  } catch (errorValue) { error.textContent = errorValue.message; }
}

async function loadAudit() {
  const list = $('#auditList');
  try {
    const { events } = await api('/api/admin/audit');
    list.replaceChildren();
    for (const event of events) {
      const row = document.createElement('div'); row.className = 'audit-row';
      const dot = document.createElement('i'); dot.className = 'audit-dot';
      const detail = document.createElement('div');
      const title = document.createElement('b'); title.textContent = event.action;
      const sub = document.createElement('small'); sub.textContent = `${event.actor_email || '系统'}${event.target ? ` · ${event.target}` : ''}`;
      detail.append(title, sub);
      const time = document.createElement('time'); time.textContent = serverDate(event.created_at)?.toLocaleString('zh-CN') || '—';
      row.append(dot, detail, time); list.append(row);
    }
    if (!events.length) list.textContent = '暂无操作记录';
  } catch { list.textContent = '无法载入审计记录'; }
}

async function loadInstances() {
  const body = $('#instancesBody');
  try {
    const { instances } = await api('/api/admin/instances');
    body.replaceChildren();
    for (const instance of instances) {
      const row = document.createElement('tr');
      row.append(makeCell(instance.email), makeCell(instance.status), makeCell(instance.flybot_version || '—'));
      const cell = makeCell('');
      for (const [action, title] of [['start', '启动'], ['stop', '停止']]) {
        const button = document.createElement('button'); button.className = 'row-action'; button.textContent = title;
        button.addEventListener('click', async () => {
          button.disabled = true;
          try { await api(`/api/admin/instances/${encodeURIComponent(instance.id)}/${action}`, { method: 'POST' }); await loadInstances(); }
          catch (error) { $('#instancesError').textContent = error.message; }
          finally { button.disabled = false; }
        });
        cell.append(button);
      }
      row.append(cell); body.append(row);
    }
    if (!instances.length) { const row = document.createElement('tr'); const cell = makeCell('暂无实例，用户可在“我的 FlyBot”中创建。'); cell.colSpan = 4; row.append(cell); body.append(row); }
  } catch (error) { $('#instancesError').textContent = error.message; }
}
async function loadAll() { await Promise.all([loadUsers(), loadAudit(), loadInstances(), loadUpgrades()]); }

async function changeStatus(userId, nextStatus) {
  try {
    await api(`/api/admin/users/${encodeURIComponent(userId)}/status`, { method: 'PATCH', body: JSON.stringify({ status: nextStatus }) });
    await loadUsers(); await loadAudit();
  } catch (error) { $('#usersError').textContent = error.message; }
}

$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault(); loginError.textContent = '';
  const form = new FormData(event.currentTarget);
  try {
    const result = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ email: form.get('email'), password: form.get('password') }) });
    csrfToken = result.csrf_token;
    if (result.user.role !== 'admin') return showLogin('此账号没有管理员权限。');
    showApp(result.user);
  } catch (error) { loginError.textContent = error.message; }
});

$('#inviteForm').addEventListener('submit', async event => {
  event.preventDefault();
  const inviteForm = event.currentTarget;
  const button = inviteForm.querySelector('button[type="submit"]');
  if (button.disabled) return;
  const form = new FormData(inviteForm);
  button.disabled = true;
  $('#usersError').textContent = '';
  try {
    const invite = await api('/api/admin/invitations', { method: 'POST', body: JSON.stringify({ email: form.get('email') }) });
    const url = `${location.origin}/dashboard/register.html#token=${encodeURIComponent(invite.token)}`;
    $('#inviteUrl').textContent = url;
    $('#inviteResult').hidden = false;
    $('#copyInvite').textContent = '复制';
    inviteForm.reset();
    await loadAudit();
  } catch (error) { $('#usersError').textContent = error.message; }
  finally { button.disabled = false; }
});

$('#copyInvite').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('#inviteUrl').textContent); $('#copyInvite').textContent = '已复制'; }
  catch { $('#copyInvite').textContent = '请手动复制'; }
});
$('#reload').addEventListener('click', loadAll);
$('#usersRefresh').addEventListener('click', loadUsers);
$('#logout').addEventListener('click', async () => { try { await api('/api/auth/logout', { method: 'POST' }); } finally { showLogin('已退出登录'); } });
const upgradePhases = { queued: '等待后台处理', checking: '检查最新 commit', fetching: '下载并校验源码', building: '构建新版镜像', canary: '检查 canary 实例', upgrading: '逐个备份并升级实例', promoting: '保存新实例默认版本', complete: '升级完成', current: '所有实例已是最新版本', failed: '升级已停止' };
const upgradeErrors = { source_unavailable: '暂时无法获取 GitHub 源码，请稍后重试。', source_invalid: '源码校验失败，本次升级已停止。', dependency_review_required: '新版依赖声明有变化，需要维护人员处理后再升级。', build_failed: '镜像构建未完成，尚未升级用户实例。', canary_failed: 'canary 检查失败，已停止后续升级；请检查后台状态。', upgrade_failed: '实例升级未完成，已停止后续升级；请联系维护人员核对恢复状态。', upgrade_failed_rollback_ready: '实例升级失败，该实例已自动回滚；后续实例未继续升级。', interrupted: '升级任务被中断，请核对实例状态后重试。', maintenance_busy: '服务器正在执行其他维护，请稍后重试。', manager_unavailable: '实例管理进程暂时不可用，请联系维护人员。', disk_reserve_low: '构建空间不足，本次升级未启动。', operation_timeout: '升级步骤超时，请联系维护人员核对状态。', rollback_failed: '自动恢复未完成，请联系维护人员处理。', release_unavailable: '所需镜像或资源不可用，请联系维护人员。' };
let upgradeRequestPending = false;
let upgradePollPending = false;
async function loadUpgrades() {
  if (!currentUser || upgradePollPending) return;
  upgradePollPending = true;
  try {
    const { job, versions } = await api('/api/admin/upgrades');
    $('#upgradeVersions').textContent = versions.length ? `当前实例版本：${versions.join('、')}` : '尚无用户实例';
    const active = job && ['queued', 'running'].includes(job.status);
    $('#upgradeFlyBot').disabled = !!active || upgradeRequestPending;
    $('#upgradeStatus').textContent = job ? upgradePhases[job.phase] || '正在处理' : '尚未发起升级';
    $('#upgradeProgress').textContent = job ? `${job.completed} / ${job.total} 个实例已处理${active && Date.now() / 1000 - job.updated_at > 120 ? ' · 后台响应较慢，请勿重复发起' : ''}` : '点击后自动检查最新版本，无需填写命令。';
    $('#upgradeError').textContent = job?.error_code ? upgradeErrors[job.error_code] || '升级未完成，请联系维护人员。' : '';
    const commit = $('#upgradeCommit'); commit.hidden = !job?.commit;
    if (job?.commit && /^[a-f0-9]{40}$/.test(job.commit)) { commit.href = `https://github.com/ThinkFlowLab/FlyBot/commit/${job.commit}`; commit.textContent = `目标 commit：${job.commit.slice(0, 8)}`; }
  } catch (error) {
    // A tunnel/browser interruption must not turn a running server job into a
    // second request. Keep the last status visible and recover on the next poll.
    $('#upgradeError').textContent = `${error.message}；将自动重连并继续读取状态`;
  }
  finally { upgradePollPending = false; }
}
$('#upgradeFlyBot').addEventListener('click', async () => {
  if (upgradeRequestPending || $('#upgradeFlyBot').disabled) return;
  upgradeRequestPending = true; $('#upgradeFlyBot').disabled = true; $('#upgradeError').textContent = '';
  let failure = '';
  try { await api('/api/admin/upgrades', { method: 'POST', body: '{}' }); }
  catch (error) {
    // POST may have reached the server even when the browser lost its reply.
    // GET is authoritative because the endpoint returns the existing active job.
    try { await loadUpgrades(); } catch { /* the poller will retry */ }
    failure = error.message;
  }
  finally {
    upgradeRequestPending = false;
    await loadUpgrades();
    if (failure && !$('#upgradeStatus').textContent.includes('升级') && !$('#upgradeStatus').textContent.includes('等待')) $('#upgradeError').textContent = failure;
  }
});
window.addEventListener('online', () => { if (currentUser) { notify('网络已恢复，正在重新读取升级状态'); loadUpgrades(); } });
setInterval(() => { if (currentUser && !appView.hidden && !document.hidden) loadUpgrades(); }, 4000);
const socialPhases = { queued: '等待后台处理', checking: '检查最新 commit', fetching: '下载源码', building: '构建新版镜像', canary: '检查隔离 canary', upgrading: '备份并切换生产容器', promoting: '保存新版本', complete: '升级完成', failed: '升级已停止' };
let socialPollPending = false;
async function loadSocialUpgrade() {
  if (!currentUser || socialPollPending) return;
  socialPollPending = true;
  try {
    const { job } = await api('/api/admin/bot-social/upgrades');
    const active = job && ['queued', 'running'].includes(job.status);
    $('#upgradeBotSocial').disabled = !!active;
    $('#socialUpgradeStatus').textContent = job ? socialPhases[job.phase] || '正在处理' : '尚未发起升级';
    $('#socialUpgradeProgress').textContent = job ? `${job.completed} / ${job.total || 1} 个阶段已完成` : '点击后自动检查最新版本。';
    $('#socialUpgradeError').textContent = job?.error_code ? `升级未完成（${job.error_code}），旧版本仍保留。` : '';
  } catch (error) { $('#socialUpgradeError').textContent = `${error.message}；将自动重连并继续读取状态`; }
  finally { socialPollPending = false; }
}
$('#upgradeBotSocial').addEventListener('click', async () => {
  if ($('#upgradeBotSocial').disabled) return;
  $('#upgradeBotSocial').disabled = true; $('#socialUpgradeError').textContent = '';
  try { await api('/api/admin/bot-social/upgrades', { method: 'POST', body: '{}' }); }
  catch (error) { $('#socialUpgradeError').textContent = `${error.message}；请等待页面自动确认后台任务`; }
  finally { await loadSocialUpgrade(); }
});
const originalLoadAll = loadAll;
loadAll = async function() { await originalLoadAll(); await loadSocialUpgrade(); };
window.addEventListener('online', () => { if (currentUser) loadSocialUpgrade(); });
setInterval(() => { if (currentUser && !appView.hidden && !document.hidden) loadSocialUpgrade(); }, 4000);
refreshSession();
