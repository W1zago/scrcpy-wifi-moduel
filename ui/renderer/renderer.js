(() => {
  const api = window.electronAPI;

  if (!api) {
    console.error('Electron API is not available.');
  }

  // Стан застосунку
  let devices = [];
  const openPairDrawers = new Set();
  let toastTimeout = null;

  // Helper: Toast повідомлення
  function showToast(text, isError = false) {
    const toast = document.getElementById('toast');
    const toastText = document.getElementById('toast-text');
    if (!toast || !toastText) return;

    toastText.textContent = text;
    toast.firstElementChild.className = isError
      ? 'px-3.5 py-1.5 rounded-md bg-card border border-danger/50 text-red-300 text-xs font-medium shadow-xl flex items-center gap-2'
      : 'px-3.5 py-1.5 rounded-md bg-card border border-bordercol text-white text-xs font-medium shadow-xl flex items-center gap-2';

    toast.classList.remove('opacity-0', 'translate-y-2');
    toast.classList.add('opacity-100', 'translate-y-0');

    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
      toast.classList.remove('opacity-100', 'translate-y-0');
      toast.classList.add('opacity-0', 'translate-y-2');
    }, 2800);
  }

  // Рендеринг списку пристроїв
  function renderDeviceList() {
    const container = document.getElementById('device-list');
    const noDevicesMsg = document.getElementById('no-devices-msg');
    const countLabel = document.getElementById('device-count-label');

    if (countLabel) {
      countLabel.textContent = `Знайдені пристрої (${devices.length})`;
    }

    if (devices.length === 0) {
      if (noDevicesMsg) noDevicesMsg.classList.remove('hidden');
      if (container) container.innerHTML = '';
      return;
    }

    if (noDevicesMsg) noDevicesMsg.classList.add('hidden');

    container.innerHTML = devices.map((device, index) => {
      const isPairing = device.kind === 'pair';
      const drawerOpen = openPairDrawers.has(index);

      if (isPairing) {
        return `
          <div class="device-card-lift bg-card rounded-lg border border-bordercol flex flex-col overflow-hidden">
            <div class="p-3.5 flex items-center justify-between gap-3">
              <div class="flex items-center gap-3 min-w-0">
                <div class="w-9 h-9 rounded-lg bg-indigo-500/10 border border-indigo-500/20 text-accent-light flex items-center justify-center shrink-0">
                  <span class="material-symbols-outlined text-[20px]">pin</span>
                </div>
                <div class="flex flex-col min-w-0">
                  <div class="flex items-center gap-2">
                    <span class="text-sm font-semibold text-white truncate">${device.name || 'Android Device'}</span>
                    <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-amber-500/10 text-amber-300 border border-amber-500/20">Парування</span>
                  </div>
                  <div class="flex items-center gap-2 text-xs font-mono text-dim mt-0.5">
                    <span>${device.ip}:${device.port}</span>
                    <span>•</span>
                    <span class="text-dim font-sans">Потрібен код підключення</span>
                  </div>
                </div>
              </div>
              <button class="toggle-pair-btn no-drag h-8 px-2.5 rounded-md bg-white/5 hover:bg-white/10 border border-bordercol text-white text-xs font-medium flex items-center gap-1 transition-colors active:scale-95 shrink-0" data-index="${index}">
                <span>Код</span>
                <span class="material-symbols-outlined text-[16px] transition-transform duration-200 ${drawerOpen ? 'rotate-180' : ''}">expand_more</span>
              </button>
            </div>

            <div class="overflow-hidden transition-all duration-200 ease-out bg-app/60 border-t ${drawerOpen ? 'max-h-40 border-bordercol-subtle' : 'max-h-0 border-transparent'}">
              <div class="p-3.5 flex flex-col gap-2.5">
                <div class="flex items-center justify-between text-xs text-dim">
                  <span>Введіть 6-значний код із «Бездротового налагодження»:</span>
                  <span class="text-[11px] font-mono text-accent-light">порт: ${device.port}</span>
                </div>
                <div class="flex items-center gap-2">
                  <input class="pair-code-input no-drag flex-1 h-8 px-3 rounded-md bg-card border border-bordercol text-white font-mono text-xs tracking-widest focus:outline-none focus:border-accent text-center"
                         maxlength="6" placeholder="000000" type="text" data-index="${index}">
                  <button class="submit-pair-btn no-drag h-8 px-3.5 rounded-md bg-accent hover:bg-accent-hover text-white text-xs font-semibold transition-all active:scale-95 shrink-0 flex items-center gap-1"
                          data-ip="${device.ip}" data-port="${device.port}" data-index="${index}">
                    <span class="material-symbols-outlined text-[14px]">link</span>
                    <span>Спарувати</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        `;
      }

      return `
        <div class="device-card-lift bg-card rounded-lg border border-bordercol p-3.5 flex items-center justify-between gap-3">
          <div class="flex items-center gap-3 min-w-0">
            <div class="w-9 h-9 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-success flex items-center justify-center shrink-0">
              <span class="material-symbols-outlined text-[20px]">smartphone</span>
            </div>
            <div class="flex flex-col min-w-0">
              <div class="flex items-center gap-2">
                <span class="text-sm font-semibold text-white truncate">${device.name || 'Android Device'}</span>
                <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-white/5 text-dim border border-bordercol">${device.kind === 'connect' ? 'Wi-Fi' : 'LAN'}</span>
              </div>
              <div class="flex items-center gap-2 text-xs font-mono text-dim mt-0.5">
                <span>${device.ip}:${device.port}</span>
                <span>•</span>
                <span class="text-success flex items-center gap-1 font-sans">
                  <span class="w-1.5 h-1.5 rounded-full bg-success"></span>
                  Online
                </span>
              </div>
            </div>
          </div>
          <button class="connect-btn no-drag h-8 px-3 rounded-md bg-accent hover:bg-accent-hover text-white text-xs font-semibold flex items-center gap-1.5 shadow-sm transition-all active:scale-95 shrink-0"
                  data-ip="${device.ip}" data-port="${device.port}">
            <span>Підключити</span>
            <span class="material-symbols-outlined text-[15px]">arrow_forward</span>
          </button>
        </div>
      `;
    }).join('');

    // Обробники кнопок "Підключити"
    container.querySelectorAll('.connect-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const button = e.currentTarget;
        const ip = button.dataset.ip;
        const port = parseInt(button.dataset.port, 10);
        showToast(`Підключення до ${ip}:${port}...`);
        if (api) {
          api.connectDevice(ip, port);
        }
      });
    });

    // Обробники кнопок розгортання панелі парування
    container.querySelectorAll('.toggle-pair-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const index = parseInt(e.currentTarget.dataset.index, 10);
        if (openPairDrawers.has(index)) {
          openPairDrawers.delete(index);
        } else {
          openPairDrawers.add(index);
        }
        renderDeviceList();
      });
    });

    // Обробники кнопок "Спарувати"
    container.querySelectorAll('.submit-pair-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const button = e.currentTarget;
        const index = button.dataset.index;
        const ip = button.dataset.ip;
        const port = parseInt(button.dataset.port, 10);
        const input = container.querySelector(`.pair-code-input[data-index="${index}"]`);
        const code = input ? input.value.trim() : '';

        if (!code || code.length < 6) {
          showToast('Введіть 6-значний код з екрану', true);
          return;
        }

        showToast(`Парування з ${ip}:${port}...`);
        if (api) {
          api.pairDevice(ip, port, code);
        }
      });
    });
  }

  // Підписка на події від Electron/Python
  if (api) {
    api.onDeviceFound((device) => {
      console.log('[Renderer] Device found:', device);
      const existingIndex = devices.findIndex(
        d => d.ip === device.ip && d.port === device.port && d.kind === device.kind
      );
      if (existingIndex >= 0) {
        devices[existingIndex] = device;
      } else {
        devices.push(device);
      }
      renderDeviceList();
    });

    api.onScanProgress((data) => {
      const bar = document.getElementById('scan-progress-bar');
      if (data && data.done && bar) {
        bar.classList.remove('animate-scan-line');
      }
    });

    api.onStatus((text) => {
      const statusEl = document.getElementById('scan-status-text');
      const footerStatus = document.getElementById('footer-status');
      if (statusEl) statusEl.textContent = text;
      if (footerStatus) footerStatus.textContent = text;
    });

    api.onConnectResult((result) => {
      if (result.success) {
        showToast(`✓ Підключено до ${result.name || result.ip + ':' + result.port}`);
      } else {
        showToast(`✗ Помилка підключення: ${result.error || 'не вдалося'}`, true);
      }
    });

    api.onPairResult((result) => {
      if (result.success) {
        showToast(`✓ Успішно спаровано з ${result.ip}:${result.port}!`);
      } else {
        showToast(`✗ Помилка парування: ${result.error || 'перевірте код'}`, true);
      }
    });

    if (api.onError) {
      api.onError((err) => {
        console.error('[Python Error]', err);
        showToast(`Помилка: ${err.message || err}`, true);
      });
    }
  }

  // Кнопка оновлення сканування
  document.getElementById('rescan-btn')?.addEventListener('click', () => {
    const icon = document.getElementById('rescan-icon');
    const bar = document.getElementById('scan-progress-bar');
    const status = document.getElementById('scan-status-text');

    if (icon) icon.classList.add('rotate-180');
    if (bar) bar.classList.add('animate-scan-line');
    if (status) status.textContent = 'Оновлення списку пристроїв...';

    devices = [];
    openPairDrawers.clear();
    renderDeviceList();

    if (api) {
      api.scanDevices();
    }

    setTimeout(() => {
      if (icon) icon.classList.remove('rotate-180');
    }, 600);
  });

  // Window controls (Titlebar)
  document.getElementById('btn-minimize')?.addEventListener('click', () => {
    if (api) api.minimizeWindow();
  });

  document.getElementById('btn-maximize')?.addEventListener('click', () => {
    if (api) api.maximizeWindow();
  });

  document.getElementById('btn-close')?.addEventListener('click', () => {
    if (api) api.closeWindow();
  });

  // Кнопка розгортання ручного вводу
  document.getElementById('btn-toggle-manual')?.addEventListener('click', () => {
    const drawer = document.getElementById('manual-drawer');
    const chevron = document.getElementById('manual-chevron');
    if (!drawer || !chevron) return;

    const isOpen = drawer.style.maxHeight && drawer.style.maxHeight !== '0px';
    if (isOpen) {
      drawer.style.maxHeight = '0px';
      drawer.classList.add('border-transparent');
      drawer.classList.remove('border-bordercol-subtle');
      chevron.classList.remove('rotate-180');
    } else {
      drawer.style.maxHeight = drawer.scrollHeight + 'px';
      drawer.classList.remove('border-transparent');
      drawer.classList.add('border-bordercol-subtle');
      chevron.classList.add('rotate-180');
    }
  });

  // Підключення вручну
  document.getElementById('manual-connect-btn')?.addEventListener('click', () => {
    const ip = document.getElementById('manual-ip')?.value.trim();
    const portStr = document.getElementById('manual-port')?.value.trim() || '5555';
    const code = document.getElementById('manual-code')?.value.trim();
    const port = parseInt(portStr, 10);

    if (!ip) {
      showToast('Вкажіть IP-адресу', true);
      return;
    }

    if (code && code.length >= 6) {
      showToast(`Парування з ${ip}:${port}...`);
      if (api) api.pairDevice(ip, port, code);
    } else {
      showToast(`Підключення до ${ip}:${port}...`);
      if (api) api.connectDevice(ip, port);
    }
  });

  // Початковий рендер списку
  renderDeviceList();
})();
