const toast = document.querySelector('#toast');
let toastTimer;
function notify(message) {
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
}

const viewNames = {
  overview: '总览', instances: '用户实例', users: '用户管理', releases: '版本与发布',
  social: 'Bot Social', audit: '操作审计', settings: '平台设置'
};
document.querySelectorAll('[data-view]').forEach(link => {
  link.addEventListener('click', event => {
    event.preventDefault();
    const view = link.dataset.view;
    document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === view));
    document.querySelector('#crumb-current').textContent = viewNames[view] || '总览';
    if (view !== 'overview') notify(`${viewNames[view]} 页面将在后续阶段接入`);
    else notify('当前显示演示数据');
  });
});

document.querySelectorAll('.filter-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.filter-tab').forEach(item => item.classList.remove('selected'));
    tab.classList.add('selected');
    const filter = tab.dataset.filter;
    document.querySelectorAll('#instanceRows tr').forEach(row => {
      row.hidden = filter !== 'all' && row.dataset.status !== filter;
    });
  });
});

document.querySelector('#searchInput').addEventListener('input', event => {
  const query = event.target.value.trim().toLocaleLowerCase();
  document.querySelectorAll('#instanceRows tr').forEach(row => {
    row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
  });
});

document.querySelector('#bannerDismiss').addEventListener('click', () => document.querySelector('.demo-banner').classList.add('hidden'));
document.querySelector('#refreshBtn').addEventListener('click', () => notify('原型未连接服务器，数据未刷新'));
document.querySelector('#newUserBtn').addEventListener('click', () => notify('邀请流程将在用户管理模块实现'));
document.querySelector('#rolloutBtn').addEventListener('click', () => notify('升级批次仅为演示，未连接任何容器'));
document.querySelectorAll('.row-menu').forEach(button => button.addEventListener('click', () => notify('实例操作尚未启用')));
