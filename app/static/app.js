const html = document.documentElement;
const toggle = document.querySelector('[data-theme-toggle]');

let theme = html.getAttribute('data-theme') || 'dark';

function applyTheme(nextTheme) {
  theme = nextTheme;
  html.setAttribute('data-theme', theme);
  try {
    localStorage.setItem('theme', theme);
  } catch (e) {}
}

if (toggle) {
  toggle.addEventListener('click', () => {
    const nextTheme = theme === 'dark' ? 'light' : 'dark';
    applyTheme(nextTheme);
  });
}

const detailCode = document.getElementById('detail-code');
const detailMeta = document.getElementById('detail-meta');
const detailWarning = document.getElementById('detail-warning');
const orderSummary = document.getElementById('order-summary');
const timeline = document.getElementById('timeline');

const EVENT_TYPE_LABELS = {
  produced: 'Производство',
  accepted: 'Приёмка',
  moved: 'Перемещение',
  shipped: 'Отгрузка',
  delivered: 'Доставка',
};

const STATUS_LABELS = {
  produced: 'Произведено',
  accepted: 'На складе',
  moved: 'Перемещено',
  shipped: 'Отгружено',
  delivered: 'У клиента',
  delayed: 'Нет подтверждения',
};

function formatDateTime(value) {
  if (!value) return '—';

  const normalized = value.includes('T') ? value : value.replace(' ', 'T');
  const date = new Date(normalized);

  if (Number.isNaN(date.getTime())) {
    return String(value).replace('T', ' ');
  }

  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

async function loadPackageDetails(code) {
  detailCode.textContent = code;
  detailMeta.textContent = 'История упаковки';
  detailWarning.innerHTML = '';
  timeline.classList.remove('hidden');
  orderSummary.innerHTML = '';

  const response = await fetch(`/api/package/${code}`);
  if (!response.ok) {
    detailMeta.textContent = 'Не удалось загрузить упаковку.';
    return;
  }

  const data = await response.json();
  const pkg = data.package;
  detailMeta.textContent = `${pkg.medicine_name} · ${pkg.batch_number} · заказ ${pkg.order_number ?? 'не назначен'}`;

  if (pkg.warning) {
    detailWarning.innerHTML = `<div class="warning-box">${pkg.warning}</div>`;
  }

  timeline.innerHTML = data.history.map((event) => `
    <article class="timeline-item">
      <div class="timeline-head">
        <strong>${event.location}</strong>
        <span class="count-pill">${formatDateTime(event.event_time)}</span>
      </div>
      <div>${EVENT_TYPE_LABELS[event.event_type] || event.event_type} · ${STATUS_LABELS[event.status] || event.status}</div>
      <div class="timeline-meta">Заказ: ${event.order_number || '—'} · ${event.counterparty || 'Без контрагента'}</div>
      <div class="timeline-meta">${event.note || 'Комментарий не указан'}</div>
    </article>
  `).join('');
}

async function loadOrderDetails(orderNumber) {
  detailCode.textContent = orderNumber;
  detailMeta.textContent = 'Загрузка состава заказа...';
  detailWarning.innerHTML = '';
  timeline.classList.add('hidden');
  timeline.innerHTML = '';

  const response = await fetch(`/api/order/${orderNumber}`);
  if (!response.ok) {
    detailMeta.textContent = 'Не удалось загрузить заказ.';
    return;
  }

  const data = await response.json();
  detailMeta.textContent = `Заказ ${data.order_number} · упаковок ${data.total_packages}`;

  const warehouseRows = Object.entries(data.warehouses)
    .map(([name, qty]) => `<li><span>${name}</span><strong>${qty}</strong></li>`)
    .join('');

  const itemsRows = data.items.map((item) => `
    <tr>
      <td>${item.package_code}</td>
      <td>${item.medicine_name}</td>
      <td>${item.batch_number}</td>
      <td>${item.last_location}</td>
      <td>${item.status_label}</td>
      <td><button class="tiny-btn js-open-package-inline" type="button" data-package-code="${item.package_code}">Открыть</button></td>
    </tr>
  `).join('');

  orderSummary.innerHTML = `
    <div class="summary-grid">
      <article class="summary-box">
        <p class="mini-title">Статус заказа</p>
        <span class="status-chip status-${data.status_tone}">${data.status_label}</span>
      </article>
      <article class="summary-box">
        <p class="mini-title">Локации</p>
        <ul>${warehouseRows}</ul>
      </article>
      <article class="summary-box">
        <p class="mini-title">Препараты</p>
        <ul>${data.medicines.map((name) => `<li><span>${name}</span></li>`).join('')}</ul>
      </article>
    </div>
    <div class="summary-table-wrap">
      <table>
        <thead>
          <tr>
            <th>Упаковка</th>
            <th>Препарат</th>
            <th>Серия</th>
            <th>Последнее место</th>
            <th>Статус</th>
            <th></th>
          </tr>
        </thead>
        <tbody>${itemsRows}</tbody>
      </table>
    </div>
  `;

  orderSummary.querySelectorAll('.js-open-package-inline').forEach((button) => {
    button.addEventListener('click', () => loadPackageDetails(button.dataset.packageCode));
  });
}

document.querySelectorAll('.js-open-order').forEach((button) => {
  button.addEventListener('click', () => loadOrderDetails(button.dataset.orderNumber));
});

const params = new URLSearchParams(window.location.search);
if (params.get('order')) {
  loadOrderDetails(params.get('order'));
}

const addOrderItemButton = document.getElementById('add-order-item');
const orderItemsContainer = document.getElementById('order-items');

function bindRemoveButtons() {
  document.querySelectorAll('.order-item-remove').forEach((button) => {
    button.onclick = () => {
      const rows = document.querySelectorAll('.order-item-row');
      if (rows.length === 1) {
        const quantityInput = rows[0].querySelector('input[name="quantity"]');
        const medicineInput = rows[0].querySelector('input[name="medicine_name"]');
        if (quantityInput) quantityInput.value = 1;
        if (medicineInput) medicineInput.value = '';
        return;
      }
      button.closest('.order-item-row')?.remove();
    };
  });
}

if (addOrderItemButton && orderItemsContainer) {
  addOrderItemButton.addEventListener('click', () => {
    const row = document.createElement('div');
    row.className = 'order-item-row';
    row.innerHTML = `
      <div class="field">
        <label>Препарат</label>
        <input name="medicine_name" list="medicine-options" placeholder="Нейрофарм" required>
      </div>
      <div class="field">
        <label>Количество упаковок</label>
        <input type="number" min="1" name="quantity" value="1" required>
      </div>
      <div class="field">
        <label>Номер упаковки</label>
        <input name="package_code" placeholder="PACK-01099 или свой код">
      </div>
      <button class="tiny-btn order-item-remove" type="button">Удалить</button>
    `;
    orderItemsContainer.appendChild(row);
    bindRemoveButtons();
  });
  bindRemoveButtons();
}

const showMoreOrdersButton = document.getElementById('show-more-orders');

if (showMoreOrdersButton) {
  showMoreOrdersButton.addEventListener('click', () => {
    document.querySelectorAll('.order-row-hidden').forEach((row) => {
      row.classList.remove('order-row-hidden');
    });
    showMoreOrdersButton.remove();
  });
}
