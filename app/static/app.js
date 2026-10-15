const html = document.documentElement;
const toggle = document.querySelector('[data-theme-toggle]');
let theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
html.setAttribute('data-theme', theme);

if (toggle) {
  toggle.addEventListener('click', () => {
    theme = theme === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', theme);
  });
}

async function loadPackageDetails(code) {
  const detailCode = document.getElementById('detail-code');
  const detailMeta = document.getElementById('detail-meta');
  const detailWarning = document.getElementById('detail-warning');
  const timeline = document.getElementById('timeline');

  detailCode.textContent = code;
  detailMeta.textContent = 'Загрузка истории...';
  detailWarning.innerHTML = '';
  timeline.innerHTML = '';

  const response = await fetch(`/api/package/${code}`);
  if (!response.ok) {
    detailMeta.textContent = 'Не удалось загрузить данные.';
    return;
  }

  const data = await response.json();
  const pkg = data.package;
  detailMeta.textContent = `${pkg.medicine_name} · ${pkg.batch_number} · ${pkg.last_location}`;

  if (pkg.warning) {
    detailWarning.innerHTML = `<div class="warning-box">${pkg.warning}</div>`;
  }

  timeline.innerHTML = data.history.map((event) => `
    <article class="timeline-item">
      <div class="timeline-head">
        <strong>${event.location}</strong>
        <span class="count-pill">${event.event_time.replace('T', ' ')}</span>
      </div>
      <div>${event.event_type} · ${event.status}</div>
      <div class="timeline-meta">${event.counterparty || 'Без контрагента'}${event.order_number ? ' · ' + event.order_number : ''}</div>
      <div class="timeline-meta">${event.note || 'Комментарий не указан'}</div>
    </article>
  `).join('');
}

document.querySelectorAll('.package-row').forEach((row) => {
  row.addEventListener('click', () => loadPackageDetails(row.dataset.packageCode));
});

const initialCode = new URLSearchParams(window.location.search).get('code');
if (initialCode) {
  loadPackageDetails(initialCode);
}
