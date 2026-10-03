const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const net = require('net');
const PythonBridge = require('./python-bridge');

let mainWindow;
let bridge = null;
const isParentPython = process.argv.includes('--parent-python');

function getPyPort() {
  const i = process.argv.indexOf('--py-port');
  if (i >= 0 && process.argv[i + 1]) {
    const p = parseInt(process.argv[i + 1], 10);
    if (p > 0 && p < 65536) return p;
  }
  return 0;
}

// Події що прийшли до готовності сторінки — в чергу, інакше webContents.send
// губить їх мовчки (бекенд сканує швидше ніж вантажиться вікно).
let pageReady = false;
let pendingEvents = [];

function deliverToWindow(channel, data) {
  if (pageReady && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, data);
    console.log('[EV -> UI]', channel);
  } else {
    pendingEvents.push({ channel, data });
    console.log('[EV queued]', channel, '(сторінка ще не готова)');
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 700,
    height: 600,
    frame: false,
    resizable: false,
    transparent: false,
    backgroundColor: '#0b0b13',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'));

  mainWindow.webContents.on('did-finish-load', () => {
    pageReady = true;
    console.log('[EV flush]', pendingEvents.length, 'подій з черги');
    for (const ev of pendingEvents) {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send(ev.channel, ev.data);
      }
    }
    pendingEvents = [];
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
    pageReady = false;
  });
}

function sendToPython(cmd) {
  if (isParentPython) {
    try {
      process.stdout.write(JSON.stringify(cmd) + '\n');
    } catch (e) {
      console.error('[Parent Python Send Error]', e);
    }
  } else if (bridge) {
    bridge.send(cmd);
  }
}

function setupBackendCommunication() {
  if (isParentPython) {
    // Події бекенд->вікно: основний канал — TCP-сокет (--py-port), бо stdin
    // в Electron на Windows глухий. Команди вікно->бекенд і далі йдуть через
    // stdout (той працює). stdin-слухач лишаємо як запасний.
    const handleBackendLine = (trimmed) => {
      if (!trimmed) return;
      try {
        const event = JSON.parse(trimmed);
        if (event && event.event) {
          console.log('[EV <- backend]', event.event);
          if (event.event === 'close-window') {
            if (mainWindow) mainWindow.close();
            return;
          }
          deliverToWindow(event.event, event.data);
        }
      } catch (e) {
        console.log('[Parent Python Log]', trimmed);
      }
    };

    const pyPort = getPyPort();
    if (pyPort) {
      const sock = net.createConnection({ host: '127.0.0.1', port: pyPort }, () => {
        console.log('[EV] py-socket connected');
      });
      let sockBuf = '';
      sock.on('data', (chunk) => {
        sockBuf += chunk.toString('utf-8');
        const lines = sockBuf.split('\n');
        sockBuf = lines.pop();
        for (const line of lines) {
          handleBackendLine(line.trim());
        }
      });
      sock.on('error', (e) => {
        console.log('[EV] py-socket error:', e.message);
      });
      sock.on('close', () => {
        console.log('[EV] py-socket closed');
      });
    }

    let buffer = '';
    process.stdin.on('data', (chunk) => {
      buffer += chunk.toString('utf-8');
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        handleBackendLine(line.trim());
      }
    });
  } else {
    const projectRoot = path.join(__dirname, '../..');
    bridge = new PythonBridge(projectRoot);

    const forwardEvents = [
      'device-found',
      'device-list',
      'scan-progress',
      'status-update',
      'connect-result',
      'pair-result',
      'error'
    ];

    for (const ev of forwardEvents) {
      bridge.on(ev, (data) => {
        console.log('[EV <- backend]', ev);
        deliverToWindow(ev, data);
      });
    }

    bridge.start();
  }
}

app.whenReady().then(() => {
  createWindow();
  setupBackendCommunication();
});

// IPC handlers для команд від UI
ipcMain.handle('scan-devices', async () => {
  sendToPython({ type: 'scan' });
});

ipcMain.handle('connect-device', async (event, { ip, port }) => {
  sendToPython({ type: 'connect', ip, port });
});

ipcMain.handle('pair-device', async (event, { ip, port, code }) => {
  sendToPython({ type: 'pair', ip, port, code });
});

// Window controls
ipcMain.on('minimize-window', () => {
  if (mainWindow) mainWindow.minimize();
});

ipcMain.on('maximize-window', () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) {
    mainWindow.unmaximize();
  } else {
    mainWindow.maximize();
  }
});

ipcMain.on('close-window', () => {
  sendToPython({ type: 'close' });
  if (bridge) bridge.stop();
  if (mainWindow) mainWindow.close();
});

app.on('window-all-closed', () => {
  if (bridge) bridge.stop();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
