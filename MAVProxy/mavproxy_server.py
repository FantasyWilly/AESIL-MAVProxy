#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
File    : mavproxy_server.py
Author  : FantasyWilly
Email   : bc697522h04@gmail.com
SPDX-License-Identifier: Apache-2.0

功能總覽:
    • MAVLink 轉發代理
    • 連接 FCU & 主 GCS (雙向轉發 UDP)
    • 建立 額外 (UDPIN, UDPOUT 通道)
    • 建立 額外 (TCP 通道)

遵循:
    • Google Python Style Guide (含區段標題)
    • PEP 8 (行寬 ≤ 88, snake_case, 2 空行區段分隔)
"""

# ------------------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------------------ #
# 標準庫
import asyncio
import logging
import sys
import os
import glob
import re
import socket
import signal
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

# 第三方套件
import yaml
from pymavlink import mavutil
from serial import SerialException


# ------------------------------------------------------------------------------------ #
# 路徑讀取
# ------------------------------------------------------------------------------------ #
def _resolve_config_path() -> str:
    # 外部環境變數讀取
    env_path = os.environ.get("MAVPROXY_CONFIG", "").strip()
    if env_path:
        return os.path.expanduser(env_path)

    # 二進制本身存在的資料夾底下的檔案
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    p = os.path.join(exe_dir, "mavproxy.yaml")
    if os.path.exists(p):
        return p

    # 當前工作空間下的檔案
    p = os.path.join(os.getcwd(), "mavproxy.yaml")
    if os.path.exists(p):
        return p

    # 跑 python3 原始碼
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "mavproxy.yaml")


CONFIG_PATH = _resolve_config_path()


# ------------------------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------------------------ #
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    cfg = {}
    logging.warning("Config file not found: %s, using built-in defaults", CONFIG_PATH)

# FCU - 硬體裝置@波特率
FCU_URL = cfg.get("serial_port", "/dev/ttyACM0")
BAUDRATE = int(cfg.get("baudrate", 57600))

# GCS URL
GCS_UDP_URL = cfg.get("gcs_url", "udpout:127.0.0.1:14550")

# UDP 通道, TCP Server
UDP_OUTS: List[Tuple[str, int]] = cfg.get("udp_outs", [("127.0.0.1", 14560)])
UDP_INS: List[Tuple[str, int]] = cfg.get("udp_ins", [("127.0.0.1", 14570)])
TCP_LISTENS: List[Tuple[str, int]] = cfg.get("tcp_listens", [("127.0.0.1", 14650)])

# 暫存器
HUB_MAXSIZE = int(cfg.get("hub_maxsize", 5))  # FCU -> hub
Q_GCS_MAXSIZE = int(cfg.get("q_gcs_maxsize", 5))  # hub -> GCS writer
Q_UDP_MAXSIZE = int(cfg.get("q_udp_maxsize", 5))  # hub -> UDP writer
PER_CLIENT_Q = int(cfg.get("tcp_send_q_maxsize", 5))  # hub -> each TCP client

# 是否踢掉同一個 IP 的重複 TCP 連線 & 最大各戶量
TCP_KICK_DUPLICATE_IP = bool(cfg.get("tcp_kick_duplicate_ip", True))
TCP_MAX_CLIENTS = int(cfg.get("tcp_max_clients", 5))

# 監控開關 & 監控資料間隔
MONITOR_ENABLE = bool(cfg.get("monitor_enable", False))
MONITOR_INTERVAL = float(cfg.get("monitor_interval", 1.0))

# LOG 相關參數
LOG_DIR_RAW = cfg.get("log_dir", "~/logs")
LOG_DIR = os.path.expanduser(LOG_DIR_RAW)
LOG_FILE_NAME = cfg.get("log_file", "mavproxy_server.log")

LOG_FILE = os.path.join(LOG_DIR, LOG_FILE_NAME)
LOG_LEVEL_NAME = str(cfg.get("log_level", "INFO")).upper()

# Task timeout（避免卡死）
QUEUE_GET_TIMEOUT_SEC = float(cfg.get("queue_get_timeout_sec", 0.5))
SOCK_RECV_TIMEOUT_SEC = float(cfg.get("sock_recv_timeout_sec", 0.5))
RECV_MATCH_TIMEOUT_SEC = float(cfg.get("recv_match_timeout_sec", 1.0))


# ------------------------------------------------------------------------------------ #
# 日誌寫入
# ------------------------------------------------------------------------------------ #
def _ensure_log_dir(log_path: str) -> None:
    d = os.path.dirname(os.path.abspath(log_path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


_ensure_log_dir(LOG_FILE)
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)

log = logging.getLogger()


# ------------------------------------------------------------------------------------ #
# TCP 獨立資料結構
# ------------------------------------------------------------------------------------ #
@dataclass
class TcpClient:
    writer: asyncio.StreamWriter
    send_q: asyncio.Queue
    drops: int = 0


# ------------------------------------------------------------------------------------ #
# 主程式 (負責管理 通訊 I/O)
# ------------------------------------------------------------------------------------ #
class MavProxyAsync:
    def __init__(self):
        # ------------------------------------------------------------------
        # 0. Stop event (Ctrl+C 一次就能乾淨停)
        # ------------------------------------------------------------------
        self._stop_event = asyncio.Event()

        # ------------------------------------------------------------------
        # 1. 定義 基礎通訊 物件 FCU & GCS
        # ------------------------------------------------------------------
        self.mav_fcu = None
        self.mav_gcs = None
        self._fcu_connected = False
        self._fcu_device = None

        # ------------------------------------------------------------------
        # 2. 建立其它程式寫入 FCU LOCK 鎖 - 資訊寫入不會打架
        # ------------------------------------------------------------------
        self._fcu_write_lock = asyncio.Lock()

        # ------------------------------------------------------------------
        # 3. 建立 HUB 暫存器 - 將 FCU 資訊傳入進 HUB
        # ------------------------------------------------------------------
        self.hub_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=HUB_MAXSIZE)

        # ------------------------------------------------------------------
        # 4. HUB 飛控資訊 傳入進 UDP & GCS
        # ------------------------------------------------------------------
        self.gcs_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=Q_GCS_MAXSIZE)
        self.udp_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=Q_UDP_MAXSIZE)

        # ------------------------------------------------------------------
        # 5. TCP 客戶連線
        # ------------------------------------------------------------------
        self.clients: Dict[int, TcpClient] = {}
        self._next_client_id = 1
        self._tcp_servers: List[asyncio.AbstractServer] = []

        # ------------------------------------------------------------------
        # 6. 紀錄丟包次數
        # ------------------------------------------------------------------
        self.drop_hub = 0
        self.drop_gcs = 0
        self.drop_udp = 0

        # UDP TX socket
        self._udp_tx_sock: Optional[socket.socket] = None

    # ==========================================================================
    # Stop control
    # ==========================================================================
    def request_stop(self) -> None:
        self._stop_event.set()

    def stopping(self) -> bool:
        return self._stop_event.is_set()

    async def wait_stopped(self) -> None:
        await self._stop_event.wait()

    # ==========================================================================
    # 初始化連接資訊 & 外部寫入 FCU 通道
    # ==========================================================================
    # ------------------------------------------------------------------
    # 連接 FCU
    # ------------------------------------------------------------------
    def _connect_fcu_blocking(self) -> None:
        # 你原本的 regex 有個小 bug：(\d+) 被寫成 (\\d+)
        m = re.match(r"^(/dev/[A-Za-z]+)(\d+)?$", FCU_URL)
        if m:
            dev_prefix = m.group(1)
            pattern = f"{dev_prefix}*"
        else:
            pattern = "/dev/ttyACM*"

        candidates = [FCU_URL] + sorted(glob.glob(pattern))
        seen = set()
        last_err = None

        for dev in candidates:
            if dev in seen:
                continue
            seen.add(dev)
            try:
                mav = mavutil.mavlink_connection(
                    device=dev,
                    baud=BAUDRATE,
                    source_system=250,
                    source_component=190,
                )
                if not mav.wait_heartbeat(timeout=5):
                    raise SerialException("No heartbeat within 5s")
                log.info("[LINK] Connected FCU: %s @ %s", dev, BAUDRATE)
                self.mav_fcu = mav
                self._fcu_device = dev
                return
            except Exception as e:
                last_err = e

        if last_err is not None:
            raise last_err
        raise SerialException("No FCU device found")

    def _connect_gcs_blocking(self) -> None:
        mav = mavutil.mavlink_connection(
            GCS_UDP_URL,
            source_system=250,
            source_component=190,
        )
        log.info("[FCU -> GCS(UDP)]: %s", GCS_UDP_URL)
        self.mav_gcs = mav

    # ------------------------------------------------------------------
    # 處理外部資訊 寫入 FCU
    # ------------------------------------------------------------------
    async def write_to_fcu(self, data: bytes) -> None:
        if not data or self.mav_fcu is None or self.stopping():
            return
        async with self._fcu_write_lock:
            try:
                await asyncio.to_thread(self.mav_fcu.write, data)
            except Exception as e:
                if self.stopping():
                    return
                log.error("Write to FCU failed: %s", e)
                await self._reconnect_fcu()

    async def _reconnect_fcu(self) -> None:
        if self.stopping():
            return
        self._fcu_connected = False
        while not self.stopping():
            try:
                await asyncio.to_thread(self._connect_fcu_blocking)
                dev = self._fcu_device or FCU_URL
                log.info("[LINK] FCU reconnected: %s @ %s", dev, BAUDRATE)
                self._fcu_connected = True
                await self.start_tcp_servers()
                return
            except Exception as e:
                log.error("FCU reconnect failed: %s", e)
                await asyncio.sleep(2)

    async def _reconnect_gcs(self) -> None:
        if self.stopping():
            return
        while not self.stopping():
            try:
                await asyncio.to_thread(self._connect_gcs_blocking)
                log.info("[LINK] GCS reconnected: %s", GCS_UDP_URL)
                return
            except Exception as e:
                log.error("GCS reconnect failed: %s", e)
                await asyncio.sleep(2)

    # ==========================================================================
    # 暫存器 I/O 管理
    # ==========================================================================
    # ------------------------------------------------------------------
    # 將 FCU 取得的資訊放入 HUB 暫存器
    # ------------------------------------------------------------------
    async def fcu_ingest_task(self) -> None:
        if not self._fcu_connected or self.mav_fcu is None:
            while not self.stopping():
                try:
                    await asyncio.to_thread(self._connect_fcu_blocking)
                    self._fcu_connected = True
                    break
                except Exception as e:
                    log.error("FCU connect failed: %s", e)
                    await asyncio.sleep(2)

        try:
            while not self.stopping():
                try:
                    msg = await asyncio.to_thread(
                        self.mav_fcu.recv_match,
                        blocking=True,
                        timeout=RECV_MATCH_TIMEOUT_SEC,
                    )
                except Exception as e:
                    raise e

                if self.stopping():
                    break
                if not msg:
                    continue

                buf = msg.get_msgbuf()
                if not buf:
                    continue

                try:
                    self.hub_q.put_nowait(buf)
                except asyncio.QueueFull:
                    try:
                        self.hub_q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self.hub_q.put_nowait(buf)
                    except asyncio.QueueFull:
                        self.drop_hub += 1

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.stopping():
                return
            log.error("FCU recv failed: %s", e)
            self._fcu_connected = False
            await self.kick_all_clients(reason="FCU disconnected")
            await self.stop_tcp_servers()
            await asyncio.sleep(0.5)
            await self._reconnect_fcu()

    # ------------------------------------------------------------------
    # 從 HUB 暫存器 -> 發送至 GCS, UDP, TCP 專門的暫存器
    # ------------------------------------------------------------------
    async def hub_broadcast_task(self) -> None:
        try:
            while not self.stopping():
                try:
                    buf = await asyncio.wait_for(
                        self.hub_q.get(), timeout=QUEUE_GET_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    continue

                # A. to GCS queue
                try:
                    self.gcs_q.put_nowait(buf)
                except asyncio.QueueFull:
                    try:
                        self.gcs_q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self.gcs_q.put_nowait(buf)
                    except asyncio.QueueFull:
                        self.drop_gcs += 1

                # B. to UDP queue
                try:
                    self.udp_q.put_nowait(buf)
                except asyncio.QueueFull:
                    try:
                        self.udp_q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self.udp_q.put_nowait(buf)
                    except asyncio.QueueFull:
                        self.drop_udp += 1

                # C. to TCP clients
                dead: List[int] = []
                for cid, c in list(self.clients.items()):
                    try:
                        c.send_q.put_nowait(buf)
                    except asyncio.QueueFull:
                        try:
                            c.send_q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            c.send_q.put_nowait(buf)
                        except asyncio.QueueFull:
                            c.drops += 1
                            dead.append(cid)

                for cid in dead:
                    await self.kick_client(cid, reason="TX queue full")

        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # GCS 暫存器 -> 發送至地面站
    # ------------------------------------------------------------------
    async def gcs_writer_task(self) -> None:
        while self.mav_gcs is None and not self.stopping():
            try:
                await asyncio.to_thread(self._connect_gcs_blocking)
            except Exception as e:
                log.error("GCS connect failed: %s", e)
                await asyncio.sleep(2)

        try:
            while not self.stopping():
                try:
                    buf = await asyncio.wait_for(
                        self.gcs_q.get(), timeout=QUEUE_GET_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    continue

                try:
                    await asyncio.to_thread(self.mav_gcs.write, buf)
                except Exception as e:
                    if self.stopping():
                        break
                    log.error("GCS write failed: %s", e)
                    await asyncio.sleep(0.5)
                    await self._reconnect_gcs()

        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # UDP 暫存器 -> 外部連接者
    # ------------------------------------------------------------------
    async def udp_writer_task(self) -> None:
        if self._udp_tx_sock is None:
            self._udp_tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for ip, port in UDP_OUTS:
                log.info("[FCU -> UDP] (TX) %s:%s", ip, port)

        try:
            while not self.stopping():
                try:
                    buf = await asyncio.wait_for(
                        self.udp_q.get(), timeout=QUEUE_GET_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    continue

                for ip, port in UDP_OUTS:
                    try:
                        self._udp_tx_sock.sendto(buf, (ip, port))
                    except Exception as e:
                        log.error("UDP sendto %s:%s failed: %s", ip, port, e)

        except asyncio.CancelledError:
            raise

    # ==========================================================================
    # 外部 GCS, UDP 寫入至 FCU
    # ==========================================================================
    # ------------------------------------------------------------------
    # GCS -> FCU (關鍵：避免 recv_msg 卡死，改用 recv_match(timeout=...))
    # ------------------------------------------------------------------
    async def gcs_to_fcu_task(self) -> None:
        while self.mav_gcs is None and not self.stopping():
            await asyncio.sleep(0.2)

        try:
            while not self.stopping():
                try:
                    msg = await asyncio.to_thread(
                        self.mav_gcs.recv_match,
                        blocking=True,
                        timeout=RECV_MATCH_TIMEOUT_SEC,
                    )
                except Exception as e:
                    if self.stopping():
                        break
                    log.error("GCS recv failed: %s", e)
                    await asyncio.sleep(0.5)
                    await self._reconnect_gcs()
                    continue

                if self.stopping():
                    break
                if not msg:
                    continue

                buf = msg.get_msgbuf()
                if buf:
                    await self.write_to_fcu(buf)

        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # UDP IN -> FCU
    # ------------------------------------------------------------------
    async def udp_in_task(self, listen_ip: str, listen_port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)

        try:
            sock.bind((listen_ip, listen_port))
        except OSError as e:
            log.error("UDP bind failed %s:%s: %s", listen_ip, listen_port, e)
            sock.close()
            return

        loop = asyncio.get_running_loop()
        log.info("[UDP -> FCU] (RX) %s:%s", listen_ip, listen_port)

        try:
            while not self.stopping():
                try:
                    data = await asyncio.wait_for(
                        loop.sock_recv(sock, 2048),
                        timeout=SOCK_RECV_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if self.stopping():
                        break
                    log.error("UDP recv failed on %s: %s", listen_port, e)
                    await asyncio.sleep(0.5)
                    continue

                if data:
                    await self.write_to_fcu(data)

        except asyncio.CancelledError:
            raise
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # ==========================================================================
    # TCP 專門 Server / Client
    # ==========================================================================
    # ------------------------------------------------------------------
    # 建立 TCP Server
    # ------------------------------------------------------------------
    async def start_tcp_servers(self):
        if self._tcp_servers or self.stopping():
            return self._tcp_servers

        servers = []
        for ip, port in TCP_LISTENS:
            srv = await asyncio.start_server(self.handle_tcp_client, ip, port)
            log.info("[TCP <-> FCU] listening %s:%s", ip, port)
            servers.append(srv)

        self._tcp_servers = servers
        return servers

    async def stop_tcp_servers(self) -> None:
        if not self._tcp_servers:
            return
        for s in self._tcp_servers:
            s.close()
            await s.wait_closed()
        self._tcp_servers = []

    # ------------------------------------------------------------------
    # TCP Client 握手
    # ------------------------------------------------------------------
    async def handle_tcp_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        cid = self._next_client_id
        self._next_client_id += 1

        peer = writer.get_extra_info("peername")
        log.info("[TCP] client connected from %s (id=%s)", peer, cid)

        if self.stopping() or not self._fcu_connected:
            log.warning("[TCP] client reject %s (FCU Not connected / stopping)", peer)
            writer.close()
            await writer.wait_closed()
            return

        if TCP_KICK_DUPLICATE_IP and peer:
            peer_ip = peer[0]
            dup_ids = []
            for eid, c in list(self.clients.items()):
                epeer = c.writer.get_extra_info("peername")
                if epeer and epeer[0] == peer_ip:
                    dup_ids.append(eid)
            for eid in dup_ids:
                await self.kick_client(eid, reason="Duplicate IP")

        if TCP_MAX_CLIENTS > 0 and len(self.clients) >= TCP_MAX_CLIENTS:
            log.warning("[TCP] client reject %s (max=%s)", peer, TCP_MAX_CLIENTS)
            writer.close()
            await writer.wait_closed()
            return

        send_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=PER_CLIENT_Q)
        self.clients[cid] = TcpClient(writer=writer, send_q=send_q)

        writer_task = asyncio.create_task(self.tcp_writer_task(cid))

        try:
            while not self.stopping():
                data = await reader.read(2048)
                if not data:
                    break
                await self.write_to_fcu(data)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("TCP RX failed (id=%s): %s", cid, e)
        finally:
            await self.kick_client(cid, reason="RX ended")
            writer_task.cancel()
            try:
                await writer_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    # ------------------------------------------------------------------
    # TCP 資料傳輸至 外部
    # ------------------------------------------------------------------
    async def tcp_writer_task(self, cid: int) -> None:
        c = self.clients.get(cid)
        if not c:
            return
        w = c.writer

        try:
            while not self.stopping():
                try:
                    buf = await asyncio.wait_for(
                        c.send_q.get(), timeout=QUEUE_GET_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    continue

                w.write(buf)
                await w.drain()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self.stopping():
                log.error("TCP TX failed (id=%s): %s", cid, e)
        finally:
            await self.kick_client(cid, reason="TX ended")

    # ------------------------------------------------------------------
    # TCP 剔除 Client
    # ------------------------------------------------------------------
    async def kick_client(self, cid: int, reason: str) -> None:
        c = self.clients.pop(cid, None)
        if not c:
            return
        peer = c.writer.get_extra_info("peername")
        log.info(
            "[TCP] client break off %s (id=%s) (%s), drops=%s",
            peer,
            cid,
            reason,
            c.drops,
        )
        try:
            c.writer.close()
            await c.writer.wait_closed()
        except Exception:
            pass

    async def kick_all_clients(self, reason: str) -> None:
        for cid in list(self.clients.keys()):
            await self.kick_client(cid, reason=reason)

    # ------------------------------------------------------------------
    # Queue monitor
    # ------------------------------------------------------------------
    async def monitor_task(self, interval_sec: float = 1.0) -> None:
        try:
            while not self.stopping():
                await asyncio.sleep(interval_sec)
                hub = self.hub_q.qsize()
                gcs = self.gcs_q.qsize()
                udp = self.udp_q.qsize()
                tcp_clients = len(self.clients)
                tcp_total = 0
                for c in self.clients.values():
                    tcp_total += c.send_q.qsize()
                print(
                    "[Q] hub={}/{} gcs={}/{} udp={}/{} tcp_total={}/{} tcp_clients={}".format(
                        hub,
                        HUB_MAXSIZE,
                        gcs,
                        Q_GCS_MAXSIZE,
                        udp,
                        Q_UDP_MAXSIZE,
                        tcp_total,
                        PER_CLIENT_Q * max(tcp_clients, 1),
                        tcp_clients,
                    )
                )
        except asyncio.CancelledError:
            raise


# ------------------------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------------------------ #
async def run() -> None:
    proxy = MavProxyAsync()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        if not proxy.stopping():
            log.info("[MAVProxy Server] stop requested")
            log.info("-" * 88)
        proxy.request_stop()

    if loop is not None:
        try:
            loop.add_signal_handler(signal.SIGINT, _on_signal)
            loop.add_signal_handler(signal.SIGTERM, _on_signal)
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, lambda *_: _on_signal())
        signal.signal(signal.SIGTERM, lambda *_: _on_signal())
    except Exception:
        pass

    # 確認第一次是否能連線 FCU
    while not proxy.stopping():
        try:
            await asyncio.to_thread(proxy._connect_fcu_blocking)
            proxy._fcu_connected = True
            break
        except Exception as e:
            log.error("FCU connect failed: %s", e)
            await asyncio.sleep(2)

    # 確認第一次是否能連線 GCS
    while not proxy.stopping():
        try:
            await asyncio.to_thread(proxy._connect_gcs_blocking)
            break
        except Exception as e:
            log.error("GCS connect failed: %s", e)
            await asyncio.sleep(2)

    if proxy.stopping():
        return

    # 啟動多線程任務
    tasks = [
        asyncio.create_task(proxy.fcu_ingest_task()),
        asyncio.create_task(proxy.hub_broadcast_task()),
        asyncio.create_task(proxy.gcs_writer_task()),
        asyncio.create_task(proxy.udp_writer_task()),
        asyncio.create_task(proxy.gcs_to_fcu_task()),
    ]

    for ip, port in UDP_INS:
        tasks.append(asyncio.create_task(proxy.udp_in_task(ip, port)))

    await proxy.start_tcp_servers()

    if MONITOR_ENABLE:
        tasks.append(asyncio.create_task(proxy.monitor_task(MONITOR_INTERVAL)))

    log.info("[MAVProxy Server] running, cancelled by Ctrl+C")
    print("-" * 88)

    try:
        await proxy.wait_stopped()
    except KeyboardInterrupt:
        proxy.request_stop()
    finally:
        # 1. 停 TCP server（避免新連線）
        try:
            await proxy.stop_tcp_servers()
        except Exception:
            pass

        # 2. 踢掉所有 TCP clients
        try:
            await proxy.kick_all_clients(reason="Server stopping")
        except Exception:
            pass

        # 3. cancel 所有 tasks
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # 4. 關閉 UDP tx socket
        if proxy._udp_tx_sock:
            try:
                proxy._udp_tx_sock.close()
            except Exception:
                pass

        # 5. 關閉 mavlink connection（很重要）
        try:
            if proxy.mav_gcs:
                proxy.mav_gcs.close()
        except Exception:
            pass
        try:
            if proxy.mav_fcu:
                proxy.mav_fcu.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("-" * 88)
        print("退出 MAVProxy Server")
