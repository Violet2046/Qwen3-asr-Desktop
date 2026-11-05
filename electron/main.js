'use strict';

const { app, BrowserWindow, Menu, dialog, ipcMain, nativeTheme, session, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const isDev = !app.isPackaged;
const EXE_NAME = 'Qwen3-ASR-Desktop.exe';

let mainWindow = null;
let backendProcess = null;
let runtimeFile = null;
let quitting = false;

/* ------------------------------------------------------------------ 后端进程 */

function resolveBackend() {
  if (app.isPackaged) {
    const dir = path.join(process.resourcesPath, 'backend');
    return { command: path.join(dir, EXE_NAME), args: [], cwd: dir };
  }
  const projectRoot = path.join(__dirname, '..');
  if (process.env.QWEN3ASR_BACKEND === 'source') {
    return {
      command: path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
      args: [path.join(projectRoot, 'app.py')],
      cwd: projectRoot
    };
  }
  const distDir = path.join(projectRoot, 'dist', 'Qwen3-ASR-Desktop');
  const distExe = path.join(distDir, EXE_NAME);
  if (fs.existsSync(distExe)) {
    return { command: distExe, args: [], cwd: distDir };
  }
  return {
    command: path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
    args: [path.join(projectRoot, 'app.py')],
    cwd: projectRoot
  };
}

function startBackend() {
  const { command, args, cwd } = resolveBackend();
  runtimeFile = path.join(os.tmpdir(), `qwen3asr-${process.pid}-${Date.now()}.json`);
  try { fs.unlinkSync(runtimeFile); } catch (error) { /* 不存在 */ }

  const fullArgs = [...args, '--no-browser', '--runtime-file', runtimeFile];
  console.log('[electron] starting backend:', command, fullArgs.join(' '));

  backendProcess = spawn(command, fullArgs, {
    cwd,
    windowsHide: true,
    stdio: 'ignore',
    env: { ...process.env, PYTHONUTF8: '1' }
  });

  backendProcess.on('error', (error) => {
    console.error('[electron] backend spawn failed:', error);
    if (!quitting) {
      dialog.showErrorBox('无法启动识别服务', `后端启动失败：${error.message}\n请确认程序目录完整。`);
      app.quit();
    }
  });

  backendProcess.on('exit', (code, signal) => {
    console.log('[electron] backend exited:', code, signal);
    if (quitting) return;
    backendProcess = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents
        .executeJavaScript(
          "document.getElementById('statusTitle')&&(document.getElementById('statusTitle').textContent='后端已退出')"
        )
        .catch(() => {});
    }
  });
}

function waitForBackend(timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const tick = () => {
      try {
        if (runtimeFile && fs.existsSync(runtimeFile)) {
          const raw = fs.readFileSync(runtimeFile, 'utf8').trim();
          if (raw) {
            const info = JSON.parse(raw);
            if (info && info.api) {
              resolve(info);
              return;
            }
          }
        }
      } catch (error) {
        /* 文件可能正在写入，继续等待 */
      }
      if (Date.now() - startedAt > timeoutMs) {
        reject(new Error('后端启动超时'));
        return;
      }
      setTimeout(tick, 250);
    };
    tick();
  });
}

function stopBackend() {
  if (backendProcess && !backendProcess.killed) {
    try { backendProcess.kill(); } catch (error) { /* ignore */ }
  }
  backendProcess = null;
  if (runtimeFile) {
    try { fs.unlinkSync(runtimeFile); } catch (error) { /* ignore */ }
    runtimeFile = null;
  }
}

/* ------------------------------------------------------------------ 窗口 */

function createWindow(apiBase) {
  mainWindow = new BrowserWindow({
    width: 1380,
    height: 900,
    minWidth: 1080,
    minHeight: 720,
    show: false,
    frame: false,
    backgroundColor: '#0a0e14',
    title: 'Qwen3-ASR Desktop',
    icon: path.join(__dirname, 'assets', 'icon.ico'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
      backgroundThrottling: false
    }
  });

  Menu.setApplicationMenu(null);
  mainWindow.loadURL(`${apiBase}/app`);
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });

  const sendMaxState = () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('window:maximized', mainWindow.isMaximized());
    }
  };
  mainWindow.on('maximize', sendMaxState);
  mainWindow.on('unmaximize', sendMaxState);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://127.0.0.1') || url.startsWith('http://localhost')) {
      return { action: 'allow' };
    }
    shell.openExternal(url);
    return { action: 'deny' };
  });

  if (isDev && process.env.ELECTRON_DEVTOOLS === '1') {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }

  // 开发自检：截图后退出（ELECTRON_SHOT=<png 路径>）
  if (process.env.ELECTRON_SHOT) {
    mainWindow.webContents.once('did-finish-load', () => {
      const delay = Number(process.env.ELECTRON_SHOT_DELAY || 4500);
      setTimeout(async () => {
        try {
          const image = await mainWindow.webContents.capturePage();
          fs.writeFileSync(process.env.ELECTRON_SHOT, image.toPNG());
          console.log('[electron] screenshot saved:', process.env.ELECTRON_SHOT);
        } catch (error) {
          console.error('[electron] screenshot failed:', error);
        }
        app.quit();
      }, delay);
    });
  }
}

/* ------------------------------------------------------------------ IPC */

function registerIpc() {
  ipcMain.on('window:minimize', () => { if (mainWindow) mainWindow.minimize(); });
  ipcMain.on('window:toggle-maximize', () => {
    if (!mainWindow) return;
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
  });
  ipcMain.on('window:close', () => { if (mainWindow) mainWindow.close(); });
  ipcMain.handle('window:is-maximized', () => (mainWindow ? mainWindow.isMaximized() : false));
  ipcMain.on('open-external', (_event, url) => {
    if (typeof url === 'string' && /^https?:\/\//i.test(url)) shell.openExternal(url);
  });
}

function grantMediaPermissions() {
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((_wc, permission, callback) => {
    if (permission === 'media' || permission === 'audioCapture' || permission === 'videoCapture') {
      callback(true);
      return;
    }
    callback(false);
  });
  ses.setPermissionCheckHandler((_wc, permission) => {
    return permission === 'media' || permission === 'audioCapture' || permission === 'videoCapture';
  });
}

/* ------------------------------------------------------------------ 生命周期 */

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    nativeTheme.themeSource = 'dark';
    registerIpc();
    grantMediaPermissions();
    startBackend();

    let info;
    try {
      info = await waitForBackend();
    } catch (error) {
      dialog.showErrorBox('启动失败', `本地识别服务未能启动：${error.message}`);
      quitting = true;
      stopBackend();
      app.quit();
      return;
    }
    createWindow(info.api);
  });

  app.on('window-all-closed', () => {
    quitting = true;
    stopBackend();
    app.quit();
  });

  app.on('before-quit', () => {
    quitting = true;
    stopBackend();
  });
}
