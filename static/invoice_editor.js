(function () {
  const form = document.getElementById('invoice-form');
  if (!form) return;

  const patientId = form.getAttribute('data-patient-id');
  const linesBody = document.getElementById('lines-body');
  const tmpl = document.getElementById('line-template');
  const addBtn = document.getElementById('add-line');
  const grandTotalEl = document.getElementById('invoice-grand-total');

  // --- helpers ---
  const UGX = (n) => `UGX ${Number(n || 0).toFixed(2)}`;

  function recalcRowTotal(tr) {
    const deleteCheck = tr.querySelector('.delete-check');
    if (deleteCheck && deleteCheck.checked) {
      tr.querySelector('.inv-total').textContent = UGX(0);
      return 0;
    }
    const qty = parseFloat(tr.querySelector('.inv-qty').value || '0');
    const price = parseFloat(tr.querySelector('.inv-price').value || '0');
    const total = (qty * price) || 0;
    tr.querySelector('.inv-total').textContent = UGX(total);
    return total;
  }

  function recalcGrand() {
    let sum = 0;
    linesBody.querySelectorAll('tr.inv-line:not(#line-template)').forEach((tr) => {
      const deleteCheck = tr.querySelector('.delete-check');
      if (deleteCheck && deleteCheck.checked) return;
      sum += recalcRowTotal(tr);
    });
    if (grandTotalEl) grandTotalEl.textContent = UGX(sum);
  }

  function clearIds(tr) {
    tr.querySelector('.hf-proc').value = '';
    tr.querySelector('.hf-item').value = '';
    tr.querySelector('.inv-kind').textContent = 'other';
  }

  // --- result rendering ---
  function renderResults(container, items) {
    container.innerHTML = '';
    if (!items || !items.length) {
      container.classList.remove('show');
      return;
    }
    items.forEach((it) => {
      const div = document.createElement('div');
      div.className = 'result-item';
      div.dataset.kind = it.kind;          // "procedure" | "drug" | "other"
      div.dataset.refId = it.ref_id;       // numeric id referenced in backend
      div.dataset.price = it.price;        // cash or insurer price
      div.textContent = it.text;
      // add a tiny pill for category
      const pill = document.createElement('span');
      pill.className = 'result-pill ' + (it.category === 'lab' ? 'pill-lab' : (it.kind === 'drug' ? 'pill-drug' : 'pill-proc'));
      pill.textContent = it.category ? it.category : it.kind;
      div.appendChild(pill);
      container.appendChild(div);
    });
    container.classList.add('show');
  }

  // --- search (debounced) ---
  const debounce = (fn, ms) => {
    let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  };

  async function runSearch(q) {
    const params = new URLSearchParams({ q });
    if (patientId) params.set('patient_id', patientId);
    const res = await fetch(`/api/search/catalog?${params.toString()}`, { credentials: 'same-origin' });
    if (!res.ok) return [];
    return await res.json();
  }

  const debouncedSearch = debounce(async (input) => {
    const q = input.value.trim();
    const menu = input.parentElement.querySelector('.inv-results');
    if (q.length < 2) {
      renderResults(menu, []);
      return;
    }
    try {
      const items = await runSearch(q);
      renderResults(menu, items);
    } catch (e) {
      renderResults(menu, []);
    }
  }, 200);

  // --- initialize existing rows ---
  function initRow(tr) {
    if (tr.id === 'line-template') return;

    const search = tr.querySelector('.inv-search');
    const menu = tr.querySelector('.inv-results');
    const desc = tr.querySelector('.inv-desc');
    const hfProc = tr.querySelector('.hf-proc');
    const hfItem = tr.querySelector('.hf-item');
    const qty = tr.querySelector('.inv-qty');
    const price = tr.querySelector('.inv-price');
    const removeBtn = tr.querySelector('.inv-remove');

    // Keep desc in sync with visible field
    search.addEventListener('input', (e) => {
      desc.value = e.target.value;
      clearIds(tr); // user is typing free text, reset ids/type until they pick from menu
      debouncedSearch(search);
    });

    search.addEventListener('focus', () => debouncedSearch(search));

    // Select from results
    menu.addEventListener('click', (e) => {
      const item = e.target.closest('.result-item');
      if (!item) return;

      const kind = item.dataset.kind;                    // procedure | drug | other
      const refId = item.dataset.refId;
      const unitPrice = parseFloat(item.dataset.price || '0');

      // set description to the visible text (strip the pill label)
      search.value = item.firstChild ? item.firstChild.textContent.trim() : item.textContent.trim();
      desc.value = search.value;

      // set ids per kind
      clearIds(tr);
      if (kind === 'procedure') {
        hfProc.value = refId;
        tr.querySelector('.inv-kind').textContent = 'procedure';
      } else if (kind === 'drug') {
        hfItem.value = refId;
        tr.querySelector('.inv-kind').textContent = 'drug';
      } else {
        tr.querySelector('.inv-kind').textContent = 'other';
      }

      // populate price if blank or zero
      const currentPrice = parseFloat(price.value || '0');
      if (!currentPrice) price.value = unitPrice.toFixed(2);

      // default qty to 1 if 0
      if (!parseFloat(qty.value || '0')) qty.value = '1';

      recalcRowTotal(tr);
      recalcGrand();

      // hide menu
      menu.classList.remove('show');
      menu.innerHTML = '';
    });

    // clicking outside closes the menu
    document.addEventListener('click', (e) => {
      if (!menu.contains(e.target) && e.target !== search) {
        menu.classList.remove('show');
      }
    });

    // qty/price recalc
    qty.addEventListener('input', () => { recalcRowTotal(tr); recalcGrand(); });
    price.addEventListener('input', () => { recalcRowTotal(tr); recalcGrand(); });

    // remove row (client side)
    removeBtn.addEventListener('click', () => {
      const deleteCheck = tr.querySelector('.delete-check');
      const isExisting = tr.getAttribute('data-existing') === '1';
      if (isExisting && deleteCheck) {
        deleteCheck.checked = true;
        tr.style.display = 'none';
      } else {
        tr.remove();
      }
      recalcGrand();
    });

    // initial total
    recalcRowTotal(tr);
  }

  linesBody.querySelectorAll('tr.inv-line').forEach(initRow);
  recalcGrand();

  // --- add line ---
  addBtn.addEventListener('click', () => {
    const clone = tmpl.cloneNode(true);
    clone.id = '';
    clone.classList.remove('d-none');
    linesBody.appendChild(clone);
    initRow(clone);
  });

  // final sanity before submit: ensure hidden desc matches visible search
  form.addEventListener('submit', () => {
    linesBody.querySelectorAll('tr.inv-line:not(#line-template)').forEach((tr) => {
      const search = tr.querySelector('.inv-search');
      const desc = tr.querySelector('.inv-desc');
      if (search && desc) desc.value = search.value || '';
    });
  });
})();
