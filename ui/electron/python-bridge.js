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
    this.process = spawn('python', [
      scriptPath,
      '--electron-mode',
      '--no-gui'
    ], {
      cwd: this.projectRoot,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1'
      }
    });

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
