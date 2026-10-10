const { spawn } = require('child_process');
const path = require('path');
const EventEmitter = require('events');

class PythonBridge extends EventEmitter {
  constructor(projectRoot) {
    super();
    this.projectRoot = projectRoot || path.join(__dirname, '../..');
    this.process = null;
    this.buffer = '';
  }

  start() {
    const scriptPath = path.join(this.projectRoot, 'tools', 'auto_run.py');
    const extraArgs = [];
    // Прокидаємо режим скану дочірньому python:
    // npm start -- --port-scan  /  electron . --port-scan
    if (process.argv.includes('--port-scan')) {
      extraArgs.push('--port-scan');
    }
    if (process.argv.includes('--ip-only')) {
      extraArgs.push('--ip-only');
    }
    // Linux/macOS: бінарник зветься python3, 'python' часто відсутній
    // (Debian/Ubuntu). На Windows — 'python' / 'py'.
    const pyCmd =
      process.env.PYTHON_BIN ||
      (process.platform === 'win32' ? 'python' : 'python3');
    const pyFallback =
      process.platform === 'win32' ? 'py' : 'python';
    this._spawnPython(pyCmd, pyFallback, scriptPath, extraArgs);
  }

  _spawnPython(pyCmd, pyFallback, scriptPath, extraArgs) {
    let cmd = pyCmd;
    // Якщо python3 нема в PATH — пробуємо fallback (python / py).
    try {
      const { spawnSync } = require('child_process');
      const check = spawnSync(cmd, ['--version'], { stdio: 'ignore', timeout: 5000 });
      if (check.error || check.status !== 0) {
        const fb = spawnSync(pyFallback, ['--version'], { stdio: 'ignore', timeout: 5000 });
        if (!fb.error && fb.status === 0) {
          console.log(`[PythonBridge] '${cmd}' недоступний, використовую '${pyFallback}'`);
          cmd = pyFallback;
        }
      }
    } catch (e) {
      // ігноруємо — спробуємо запустити як є, помилку буде видно в 'error'
    }
    this.process = spawn(cmd, [
      scriptPath,
      '--electron-mode',
      '--no-gui',
      ...extraArgs
    ], {
      cwd: this.projectRoot,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1'
      }
    });
    this.process.on('error', (err) => {
      // Типово Linux: ENOENT коли нема 'python' (є тільки 'python3') і навпаки.
      console.error(`[PythonBridge] не вдалося запустити '${cmd}': ${err.message}`);
      if (cmd !== pyFallback) {
        console.log(`[PythonBridge] пробую fallback '${pyFallback}'...`);
        this._spawnPython(pyFallback, pyFallback, scriptPath, extraArgs);
        return;
      }
      this.emit('python-error', `Python не знайдено ('${cmd}'): ${err.message}`);
    });
    if (!this.process.stdout || !this.process.stderr) {
      // spawn не вдався (ENOENT) — fallback вже запущено вище, слухачі не потрібні
      return;
    }

    this.process.stdout.on('data', (data) => {
      this.buffer += data.toString();
      const lines = this.buffer.split('\n');
      this.buffer = lines.pop(); // save incomplete line

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          const event = JSON.parse(trimmed);
          if (event && event.event) {
            this.emit(event.event, event.data);
          }
        } catch (e) {
          console.log('[Python Log]', trimmed);
          this.emit('log', trimmed);
        }
      }
    });

    this.process.stderr.on('data', (data) => {
      const errStr = data.toString().trim();
      if (errStr) {
        console.error('[Python Error]', errStr);
        this.emit('python-error', errStr);
      }
    });

    this.process.on('close', (code) => {
      console.log(`[Python Process] exited with code ${code}`);
      this.emit('exit', code);
    });
  }

  send(command) {
    if (this.process && this.process.stdin && this.process.stdin.writable) {
      this.process.stdin.write(JSON.stringify(command) + '\n');
    } else {
      console.warn('[PythonBridge] Cannot send command, stdin not writable:', command);
    }
  }

  scan() {
    this.send({ type: 'scan' });
  }

  connect(ip, port) {
    this.send({ type: 'connect', ip, port });
  }

  pair(ip, port, code) {
    this.send({ type: 'pair', ip, port, code });
  }

  stop() {
    if (this.process) {
      try {
        this.process.kill();
      } catch (e) {
        // ignore
      }
      this.process = null;
    }
  }
}

module.exports = PythonBridge;
