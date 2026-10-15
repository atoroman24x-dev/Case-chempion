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
  const title = document.getElementById('detail-title');
  const status = document.getElementById('detail-status');
  const summary = document.getElementById('detail-summary');
  const warning = document.getElementById('detail-warning');
  const timeline = document.getElementById('timeline');

  title.textContent = code;
  status.textContent = 'загрузка';
  summary.textContent = 'Получаем историю и текущее состояние упаковки...';
  warning.innerHTML = '';
  timeline.innerHTML = '';

  const response = await fetch(`/api/package/${code}`);
  if (!response.ok) {
    status.textContent = 'ошибка';
    summary.textContent = 'Не удалось загрузить данные по упаковке.';
    return;
  }

  const data = await response.json();
  const pkg = data.package;

  title.textContent = `${pkg.package_code}`;
  status.textContent = pkg.status_label;
  status.className = `pill status-${pkg.status_color}`;
  summary.innerHTML = `
    <div class="summary-grid">
      <div><span>Препарат</span><strong>${pkg.medicine_name}</strong></div>
      <div><span>Серия</span><strong>${pkg.batch_number}</strong></div>
      <div><span>Где сейчас</span><strong>${pkg.last_location}</strong></div>
      <div><span>Последнее событие</span><strong>${pkg.last_event_label}</strong></div>
      <div><span>Подтверждено</span><strong>${pkg.last_event_time}</strong></div>
      <div><span>Количество событий</span><strong>${pkg.events_count}</strong></div>
    </div>
  `;

  if (pkg.warning) {
    warning.innerHTML = `<div class="alert-row">${pkg.warning}</div>`;
  }

  timeline.innerHTML = data.history.map((event, index) => `
    <article class="timeline-item ${index === 0 ? 'is-current' : ''}">
      <div class="timeline-head">
        <div>
          <strong>${event.event_label}</strong>
          <p>${event.location}</p>
        </div>
        <span class="pill status-${event.status_color}">${event.status_label}</span>
      </div>
      <div class="timeline-info">
        <div><span>Дата</span><strong>${event.event_time}</strong></div>
        <div><span>Контрагент</span><strong>${event.counterparty || 'Не указан'}</strong></div>
        <div><span>Документ</span><strong>${event.order_number || '—'}</strong></div>
      </div>
      <div class="timeline-note">${event.note || 'Комментарий отсутствует'}</div>
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
