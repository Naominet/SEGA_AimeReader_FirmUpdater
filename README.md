# SEGA AIME 读卡器工具 使用文档

## 概述

用于管理 SEGA 街机 AIME NFC 读卡器的命令行工具，支持固件更新、版本查询、LED 控制和读卡测试。

### 支持的读卡器型号

| 型号 | 世代 | 硬件版本字符串 | 固件版本 |
|------|------|----------------|----------|
| 837-15084 (TN32MSEC003S) | Gen1 | `TN32MSEC003S H/W Ver3.0` | `TN32MSEC003S F/W Ver1.2` |
| 837-15286 | Gen2 | `837-15286` | `0x94` |
| 837-15396 | Gen3 | `837-15396` | `0x94` |

### 依赖

- Python 3.8+
- pyserial >= 3.5

```bash
pip install pyserial
```

---

## 全局选项

所有命令都支持以下选项：

| 选项 | 缩写 | 说明 | 默认值 |
|------|------|------|--------|
| `--port PORT` | `-p` | 串口号 (如 COM1, /dev/ttyUSB0) | 必填 (或在 config.ini 中配置) |
| `--baud RATE` | `-b` | 波特率 | 115200 |
| `--verbose` | `-v` | 显示详细调试信息 (含 TX/RX hex dump) | 关 |
| `--gen {1,2,3}` | | 手动指定读卡器世代 | 自动检测 |

---

## 命令列表

### 1. `info` — 查询读卡器信息

查询并显示读卡器的固件版本、硬件版本、型号和世代。如果当前波特率无响应，会自动尝试另一种波特率 (115200/38400)。

```bash
python main.py --port COM1 info
```

**输出示例：**
```
=== AIME Reader Info ===

Resetting reader...
Querying version info...

  Hardware Version: 837-15396
  Firmware Version: 0x94
  Model:            837-15396
  Generation:       3
```

---

### 2. `diag` — 串口诊断

底层诊断工具，用于排查连接问题。会列出所有可用串口、发送原始协议帧并显示读卡器返回的每一个字节。

```bash
python main.py --port COM1 diag
```

**功能：**
- 列出系统所有可用 COM 口 (含设备描述和 VID/PID)
- 检测串口残留数据
- 发送 RESET、GET_FW_VERSION、GET_HW_VERSION 原始帧
- 显示完整的 TX/RX hex dump
- 汇总诊断结果

**输出示例：**
```
=== Serial Diagnostics ===
  Port: COM1  Baud: 115200

[0] Available serial ports:
    COM1: Silicon Labs CP210x USB to UART Bridge (COM1) [USB VID:PID=10C4:EA60]

[1] Reading stale data (1s)...
  (empty — no stale data)

[2] Sending RESET command (cmd=0x62 payload=03)...
  TX frame: e0 05 00 00 62 03 6a
  Waiting for response (3s)...
  RX: (no data — reader did not respond)

[3] Sending GET_FW_VERSION (cmd=0x30 payload=00)...
  TX frame: e0 05 00 01 30 00 36
  Waiting for response (2s)...
  RX (9 bytes): e0 07 00 01 30 00 01 94 cd

[4] Sending GET_HW_VERSION (cmd=0x32 payload=00)...
  TX frame: e0 05 00 02 32 00 39
  Waiting for response (2s)...
  RX (17 bytes): e0 0f 00 02 32 00 09 38 33 37 2d 31 35 33 39 36 23

--- Summary ---
  Reader IS responding on COM1 at 115200 baud.
```

---

### 3. `update` — 固件更新

将固件文件写入读卡器。支持 `.bin` (ARM Cortex-M0 原始二进制) 和 `.hex` (Intel HEX) 格式。

```bash
# 基本用法 (会提示确认)
python main.py --port COM1 update -f firmware.bin

# 更新后验证版本
python main.py --port COM1 update -f firmware.bin --verify

# 跳过确认和兼容性检查
python main.py --port COM1 update -f firmware.hex --force

# 同时启用验证和强制
python main.py --port COM1 update -f firmware.bin --verify --force
```

| 选项 | 说明 |
|------|------|
| `-f FILE`, `--firmware FILE` | 固件文件路径 (必填) |
| `--verify` | 更新后重新查询版本，确认更新成功 |
| `--force` | 跳过确认提示和型号兼容性检查 |

**更新流程：**
1. 加载固件文件，显示大小和校验和
2. 查询当前读卡器型号和版本
3. 检查固件与硬件兼容性
4. 关闭射频
5. 进入固件更新模式 (cmd 0x60)
6. 分包发送固件数据，显示进度条
   - Gen1: Intel HEX 记录，43 字节/包 (cmd 0x61)
   - Gen2/3: 二进制 256 字节/包 (cmd 0x63 初始化 + cmd 0x64 传输)
7. 复位读卡器 (cmd 0x62)
8. (可选) 重新查询版本验证

**兼容性规则：**
- Gen1 (TN32MSEC003S, ATmega) → 使用 `.hex` 固件 (Intel HEX)
- Gen2/Gen3 (837-15286/837-15396, LPC1112 ARM) → 使用 `.bin` 固件 (原始二进制, 12288 bytes)
- `--force` 可跳过此检查

---

### 4. `led` — LED 控制

控制读卡器上的 RGB LED 灯环。

#### 预设颜色

```bash
python main.py --port COM1 led --color red
python main.py --port COM1 led --color green
python main.py --port COM1 led --color blue
python main.py --port COM1 led -c white
```

可用颜色：`red`, `green`, `blue`, `white`, `yellow`, `cyan`, `magenta`, `orange`, `purple`, `pink`, `off`

#### 自定义 RGB

```bash
python main.py --port COM1 led --rgb 255 0 128
python main.py --port COM1 led --rgb 0 255 0
python main.py --port COM1 led --rgb 100 100 255
```

#### 闪烁效果

```bash
# 默认闪 3 次
python main.py --port COM1 led --flash red

# 闪 5 次
python main.py --port COM1 led --flash blue --times 5
```

| 选项 | 说明 |
|------|------|
| `--flash COLOR` | 用指定颜色闪烁 |
| `--times N` | 闪烁次数 (默认 3) |

#### 彩虹效果

```bash
# 默认 5 秒彩虹
python main.py --port COM1 led --rainbow

# 10 秒彩虹
python main.py --port COM1 led --rainbow --duration 10
```

| 选项 | 说明 |
|------|------|
| `--rainbow` | 彩虹色循环动画 |
| `--duration SEC` | 动画持续时间 (默认 5.0 秒) |

#### 关灯

```bash
python main.py --port COM1 led --off
```

---

### 5. `scan` — 扫描卡片

扫描读卡器上的 NFC 卡片 (MIFARE / FeliCa)。

```bash
# 单次扫描
python main.py --port COM1 scan

# 持续扫描 (Ctrl+C 停止)
python main.py --port COM1 scan --continuous

# 持续扫描 30 秒
python main.py --port COM1 scan --continuous --duration 30
```

| 选项 | 说明 |
|------|------|
| `--continuous` | 持续轮询模式 |
| `--duration SEC` | 持续扫描时长，0 表示无限 (默认 0) |

**输出示例 (持续模式)：**
```
=== Card Scanner ===

Initializing reader...
Enabling radio...
Scanning for cards (duration=0s, interval=0.5s)...
Press Ctrl+C to stop.

[01:23:45] MIFARE: UID=A1 B2 C3 D4
[01:23:48] Card removed
[01:23:52] FeliCa: UID=01 23 45 67 89 AB CD EF  AccessCode=02FE0123456789ABCDEF
```

---

### 6. `read-card` — 读取 AIME 卡

完整读取一张 AIME 卡的 Access Code (访问码)。

```bash
python main.py --port COM1 read-card
```

**MIFARE 卡读取流程：**
1. Poll 检测卡片，获取 UID
2. Select Tag
3. 设置 BANA 密钥 (`57 43 43 46 76 32`)
4. 认证 Block 2
5. 读取 Block 2，前 10 字节为 Access Code

**FeliCa 卡读取：**
1. Poll 检测卡片，获取 IDm
2. Access Code = `02 FE` + IDm (8 字节)

**输出示例：**
```
=== Card Reader ===

Initializing reader...
Enabling radio...
Waiting for card... (place card on reader)
  Card type: MIFARE
  UID: A1 B2 C3 D4
  Selecting tag...
  Setting BANA key...
  Authenticating block 2...
  Reading block 2...
  Access Code: 0123456789ABCDEF0123
  Block 2 raw: 01 23 45 67 89 AB CD EF 01 23 00 00 00 00 00 00

  Result: 0123456789ABCDEF0123
```

---

## 配置文件

可以在 `config.ini` 中设置默认参数，避免每次手动指定：

```ini
[DEFAULT]
# 串口号
port = COM1

# 波特率: 115200 (新款) 或 38400 (旧款)
baudrate = 115200

# 详细输出
verbose = false
```

配置后，可以省略 `--port`：
```bash
python main.py info
python main.py led --color red
python main.py scan --continuous
```

---

## 文件结构

```
sega_0855_aime_reader_firm_updater/
├── main.py              # CLI 入口，命令行参数解析
├── sg_protocol.py       # SG 帧协议编解码 (SYNC/转义/校验和)
├── sg_serial.py         # 串口通信封装 (pyserial, 自动序列号)
├── nfc_reader.py        # NFC 读卡器命令层 (全部 14 条命令)
├── led_controller.py    # LED 控制 (颜色/闪烁/彩虹)
├── firmware.py          # 固件文件加载 (.bin/.hex)
├── updater.py           # 固件更新状态机 (分包/进度条/验证)
├── card_test.py         # 读卡测试 (MIFARE/FeliCa)
├── config.ini           # 默认配置
└── requirements.txt     # 依赖: pyserial>=3.5
```

---

## 通信协议

### SG 帧格式

```
[0xE0] [frame_len] [addr] [seq] [cmd] [payload...] [checksum]
 SYNC   长度        地址   序列号 命令   载荷          校验和
```

- **frame_len** = `4 + len(payload)`（从 addr 到 checksum 的字节数）
- **checksum** = `sum(frame_len, addr, seq, cmd, payload...) & 0xFF`
- **字节转义** (SYNC 之后的所有字节)：`0xE0 → 0xD0 0xDF`，`0xD0 → 0xD0 0xCF`
- **反转义**：`0xD0 xx → (xx + 1)`

### 命令一览

| 命令 | 字节 | 请求 payload | 响应 payload |
|------|------|-------------|-------------|
| RESET | `0x62` | `[0x03]` | (可能无响应) |
| GET_FW_VERSION | `0x30` | `[0x00]` | `[len] [ASCII...]` 或 `[0x01] [ver_byte]` |
| GET_HW_VERSION | `0x32` | `[0x00]` | `[len] [ASCII...]` |
| RADIO_ON | `0x40` | `[type]` (3=双模) | (status) |
| RADIO_OFF | `0x41` | `[0x00]` | (status) |
| POLL | `0x42` | `[0x00]` | `[data_len] [count] [type] [uid_len] [uid...]` |
| MIFARE_SELECT | `0x43` | `[uid...]` | (status) |
| MIFARE_SET_KEY_BANA | `0x50` | — | — |
| MIFARE_READ_BLOCK | `0x52` | `[data_len] [uid...] [block]` | `[0x10] [16 bytes]` |
| MIFARE_SET_KEY_AIME | `0x54` | `[0x60] [6 byte key]` | (status) |
| MIFARE_AUTHENTICATE | `0x55` | `[block] [uid...]` | (status) |
| SEND_HEX_DATA | `0x61` | `[firmware chunk]` | status=0x20 表示成功 |
| FELICA_ENCAP | `0x71` | `[len] [IDm(8)] [felica_cmd_data...]` | `[resp_data...]` |
| LED_SET_COLOR | `0x81` | `[board_id] [R] [G] [B]` | (通常无响应) |

### LED 地址

LED 命令使用 `addr=0x08`，其他命令使用 `addr=0x00`。

---

## 故障排查

| 症状 | 可能原因 | 解决方法 |
|------|---------|---------|
| 完全无响应 | 串口号错误 | 运行 `diag` 查看可用端口列表 |
| 完全无响应 | 读卡器未供电 | 检查 5V/12V 电源连接 |
| 完全无响应 | TX/RX 接反 | 交换 TX 和 RX 线 |
| 收到乱码 | 波特率不对 | 尝试 `--baud 38400` |
| Reset 无响应但其他命令正常 | 正常现象 | 部分读卡器 Reset 不回复 |
| info 显示 "Unknown" 型号 | 新型号未收录 | 检查 HW 版本字符串 |
| 固件更新中断 | 连接不稳定 | 确保供电稳定，勿拔线 |
