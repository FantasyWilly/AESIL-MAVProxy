# <div align="center">AESIL - MAVProxy</div>

## <div align="center">Outline</div>

- [系統環境 (Environment)](#environment)
- [專案下載 (Downloads)](#downloads)
- [依賴項 (Dependencies)](#dependencies)
- [開啟伺服器 (MAVProxy Server)](#mavproxy-server)

## <div align="center">Environment</div>

> ★ Linux 系統 (本次專案系統環境)

```bash
# [操作系統] (Optional)
Ubuntu 22.04 LTS

# [Python 環境]
Python >= 3.8

# [其它第三方套件]
pymavlink>=2.4
pyserial>=3.5
PyYAML>=6.0
```
---

## <div align="center">Downloads</div>

```bash
git clone https://github.com/FantasyWilly/AESIL-MAVProxy.git
```
---

## <div align="center">Dependencies</div>

> ★ Linux 系統 (本專案其它套件)

```bash
cd  AESIL-MAVProxy
pip install -r requirements.txt
```

---

## <div align="center">MAVProxy Server</div>

  ### Python3 啟動

  ```bash
  cd AESIL-MAVProxy/MAVProxy
  python3 mavproxy_server.py
  ```

  ---




