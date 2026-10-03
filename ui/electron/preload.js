const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Команди від UI до Python
  scanDevices: () => ipcRenderer.invoke('scan-devices'),
  connectDevice: (ip, port) => ipcRenderer.invoke('connect-device', { ip, port }),
  pairDevice: (ip, port, code) => ipcRenderer.invoke('pair-device', { ip, port, code }),

  // Події від Python до UI
  onDeviceFound: (callback) => ipcRenderer.on('device-found', (_, device) => callback(device)),
  onDeviceList: (callback) => ipcRenderer.on('device-list', (_, list) => callback(list)),
  onScanProgress: (callback) => ipcRenderer.on('scan-progress', (_, data) => callback(data)),
  onStatus: (callback) => ipcRenderer.on('status-update', (_, text) => callback(text)),
  onConnectResult: (callback) => ipcRenderer.on('connect-result', (_, result) => callback(result)),
  onPairResult: (callback) => ipcRenderer.on('pair-result', (_, result) => callback(result)),
  onError: (callback) => ipcRenderer.on('error', (_, err) => callback(err)),

  // Window controls
  minimizeWindow: () => ipcRenderer.send('minimize-window'),
  maximizeWindow: () => ipcRenderer.send('maximize-window'),
  closeWindow: () => ipcRenderer.send('close-window')
});
