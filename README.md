# scrcpy-wifi-module

<p align="left">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square" alt="License" height="20">
  <img src="https://img.shields.io/badge/Platform-Windows-blue.svg?style=flat-square" alt="Platform" height="20">
  <img src="https://img.shields.io/badge/scrcpy-compatible-brightgreen.svg?style=flat-square" alt="Scrcpy" height="20">
</p>

> **Virtual USB-over-IP Cable Bridge for scrcpy**  
> High-performance transport layer that relays ADB & USB packets over Wi-Fi, emulating a physical USB Android connection on Windows Host.

---

## 📌 Overview

**scrcpy-wifi-module** is an automated network transport bridge designed to eliminate physical USB tethering while using [scrcpy](https://github.com/Genymobile/scrcpy). By tunneling Android USB packets over Wi-Fi and managing low-level host device bindings, it allows seamless, low-latency screen mirroring and device control.

---

## ✨ Key Features

- ⚡ **Zero-Configuration Launcher**: Auto-detects local Android devices, handles IP lookup, and starts `scrcpy` in one step.
- 📶 **Wireless ADB Pairing (Android 11+)**: Built-in mDNS discovery for automatic pairing and instant reconnection without requiring initial USB cabling.
- 🎯 **Virtual USB Emulation**: Emulates host-side USB device bindings to maintain reliable communication.
- 📊 **Adaptive Streaming & Telemetry**: Dynamic network analysis (ping, jitter, loss measurement) that auto-adjusts bitrate, resolution, and frame rates for Wi-Fi hotspots or unstable networks.
- 🔄 **State Persistence**: Remembers previously paired devices for background auto-reconnect.
- 🧪 **Simulation Mode**: Integrated test environment with mock ADB daemons for testing without physical hardware.

---

## 🏗️ Principle of Operation

`scrcpy-wifi-module` orchestrates the complete lifecycle of a virtual USB-over-IP connection through three main layers: discovery & telemetry, virtual host driver emulation, and client execution.

### Architecture Overview

```mermaid
flowchart TD
    subgraph Device ["Android Device Layer"]
        ADBD["ADB Daemon (adbd)"]
        mDNS["mDNS Wireless Pairing Service"]
    end

    subgraph Network ["Transport & Telemetry Layer"]
        Discovery["Device Discovery & Pairing Manager"]
        NetworkEngine["Transport & Jitter Control Engine"]
        Telemetry["Link Metrics & Adaptive Quality Resolver"]
    end

    subgraph Host ["Windows Host Emulation Layer"]
        VHCI["Virtual USB Host Controller (VHCI)"]
        ScrcpyClient["scrcpy Client Instance"]
    end

    Device -->|Broadcasts Service| Discovery
    Discovery -->|Establishes Socket| NetworkEngine
    Telemetry -->|Monitors RTT & Loss| NetworkEngine
    NetworkEngine -->|Encapsulates URB Packets| VHCI
    VHCI -->|Exposes Virtual USB Bus| ScrcpyClient
```

### Execution & Control Workflow

When a connection is initiated, the system executes the following operational pipeline:

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant App as Automation Launcher
    participant Discovery as mDNS & Wireless Resolver
    participant Transport as Network Transport Layer
    participant Driver as Virtual USB Controller
    participant Scrcpy as scrcpy Client Process

    User->>App: Launch Connection (START.bat)
    App->>Discovery: Discover Wireless Devices (mDNS/Pairing)
    Discovery-->>App: Device IP & Port Resolved
    App->>Transport: Initialize Transport & Telemetry Engine
    Transport->>Driver: Bind Virtual USB Device Descriptors
    Driver-->>App: Virtual USB Device Ready (VID_18D1)
    App->>Scrcpy: Spawn scrcpy Instance
    activate Scrcpy
    Scrcpy->>Driver: Issue USB IOCTL Bulk Requests
    Driver->>Transport: Bridge Packet Stream over Network
    Transport->>Scrcpy: Stream H.264/H.265 Frame Packets
    User->>Scrcpy: Terminate Session
    Scrcpy-->>App: Process Exit
    deactivate Scrcpy
    App->>Transport: Close Socket & Unbind Virtual USB
```

---

## 🚀 Quick Start

### Prerequisites

- **Host OS**: Windows 10/11 (64-bit)
- **Dependencies**:
  - `Python 3.8+`
  - `CMake 3.15+`
  - `scrcpy` and `adb` available in system `PATH`

---

### Basic Usage

#### Option A: One-Click Auto Run (Recommended)

Run via Windows script:

```cmd
START.bat
```

Window with the phone list opens first; closed it without a choice —
the script continues automatically in the console.
Console-only mode (never opens a window):

```cmd
START_CONSOLE.bat
```

#### Option B: Advanced Command Line Interface

```powershell
# Auto-detect local phone and start scrcpy
python tools/auto_run.py

# Wireless pair via 6-digit Android code
python tools/auto_run.py --pair-code 123456

# Force quality profile (Ideal for 2.4 GHz Wi-Fi / Hotspots)
python tools/auto_run.py --quality low

# Run simulation test environment (No phone connected)
python tools/auto_run.py --mode demo
```

---

## 🛠️ Performance Tuning

For minimum latency and high-framerate performance:

1. **5 GHz Band**: Ensure both the PC and Android device are connected to a 5 GHz Wi-Fi network.
2. **Mobile Hotspot**: If using Windows Hotspot, configure the band to 5 GHz and disable *Power Saving* options for the host network adapter.
3. **Quality Profile**: Use `--quality low` or `--quality medium` on congested wireless networks to prevent buffer bloat.

---

## 📂 Project Structure

```text
scrcpy-wifi-module/
├── src/                # Core C/C++ engine, driver interop, and network stack
├── ui/                 # Status GUI, connection dialogs, and monitor widgets
├── tools/              # CLI runner, automation tools, and network utilities
├── scripts/            # Build utilities and helper scripts
├── tests/              # Unit tests, mock daemons, and simulation suites
├── CMakeLists.txt      # Root CMake configuration
└── START.bat           # Launcher script for Windows
```

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for details.
