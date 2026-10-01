const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const PythonBridge = require('./python-bridge');

let mainWindow;
let bridge = null;
const isParentPython = process.argv.includes('--parent-python');

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

  mainWindow.on('closed', () => {
    mainWindow = null;
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
    let buffer = '';
    process.stdin.on('data', (chunk) => {
      buffer += chunk.toString('utf-8');
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          const event = JSON.parse(trimmed);
          if (event && event.event) {
            if (event.event === 'close-window') {
              if (mainWindow) mainWindow.close();
              return;
            }
            if (mainWindow && !mainWindow.isDestroyed()) {
              mainWindow.webContents.send(event.event, event.data);
            }
          }
        } catch (e) {
          console.log('[Parent Python Log]', trimmed);
        }
      }
    });
  } else {
    const projectRoot = path.join(__dirname, '../..');
    bridge = new PythonBridge(projectRoot);

    const forwardEvents = [
      'device-found',
      'scan-progress',
      'status-update',
      'connect-result',
      'pair-result',
      'error'
    ];

    for (const ev of forwardEvents) {
      bridge.on(ev, (data) => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send(ev, data);
        }
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
