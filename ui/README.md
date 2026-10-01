# Virtual USB Cable — Electron UI

Графічний інтерфейс користувача для бездротового підключення Android пристроїв до `scrcpy` (CyberDeck / Dark Theme).

## Архітектура

- **Frontend:** HTML5, Tailwind CSS, Material Symbols, Vanilla JS
- **Desktop Shell:** Electron (Frameless вікно 700x600)
- **IPC міст:** `preload.js` (через `contextBridge` + `ipcRenderer` / `ipcMain`)
- **Backend:** Python (`tools/auto_run.py --electron-mode --no-gui`) через двосторонній JSON-потік (stdin / stdout)

## Структура папки `ui`

```
ui/
├── electron/
│   ├── main.js              # Головний процес Electron (управління вікном, запуск Python)
│   ├── preload.js           # Безпечний IPC-міст для Renderer процесу
│   └── python-bridge.js     # Клас-обгортка для комунікації з tools/auto_run.py
├── renderer/
│   ├── index.html           # Головна розмітка (адаптована з design/code.html)
│   ├── renderer.js          # Логіка подій, динамічний рендеринг пристроїв
│   └── styles.css           # Стилі (перетягування вікна, скролбар, анімації)
├── package.json             # NPM конфігурація та залежності Electron
└── README.md                # Ця документація
```

## Встановлення та запуск

1. Встановіть залежності:
   ```bash
   cd ui
   npm install
   ```
   *(На Windows у PowerShell за потреби використовуйте `npm.cmd install`)*

2. Запустіть додаток:
   ```bash
   npm start
   ```

## Підтримувані команди IPC

- `scan`: запустити пошук телефонів по mDNS (Бездротове налагодження) та локальній мережі (порт 5555).
- `connect`: швидке підключення до пристрою за IP та портом (`adb_connect_fast`).
- `pair`: парування за 6-значним кодом з екрану телефону (`adb_pair_verify`) з автоматичним виявленням порту з'єднання.
