# Wio Tracker L2 Pro — Meshtastic 端侧语音转文字

在 **Seeed Wio Tracker L2 Pro**（ESP32-S3）上，给 Meshtastic 固件加一个**完全离线**的英文语音转文字（STT）功能：
按住屏幕上的麦克风按钮说话 → 松开 → 设备本地把语音转成文字 → 确认后作为普通文本消息广播到频道。

不联网、不上传、没有云端。

## 功能

- **端侧推理**：5.9 MB 的 NeMo Conformer-CTC（int8）模型跑在 ESP32-S3 上，权重常驻 flash（mmap），不进 RAM
- **触摸交互**：LVGL 顶层叠加的 `MIC` 圆钮（按住说话，显示计时），松手后弹出 `SEND / REDO` 确认条
- **物理按键兜底**：GPIO0（USER 键）按住说话同样有效
- **不阻塞系统**：采集与解码各跑在独立任务里，说话期间 LoRa 与 UI 全程正常
- **消息链路**：转写结果走标准的 `TEXT_MESSAGE_APP`，本机聊天列表右侧显示，同频道其它节点也能收到

## 硬件

| 项 | 规格 |
|---|---|
| MCU | ESP32-S3，240 MHz，双核 |
| 内存 | 512 KB SRAM + 8 MB OPI PSRAM |
| Flash | 16 MB |
| 屏幕 | NV3031B 320×240 横屏（LVGL / MUI） |
| 触摸 | GT911（I2C0） |
| 麦克风 | ES7243E（I2S，16 kHz 单声道） |
| 喇叭 | ES8311（与麦克风共用 MCLK/BCK/WS） |
| LoRa | SX1262 |

## 它是怎么工作的

```
ES7243E ──I2S──▶ 8 秒 PCM 缓冲(PSRAM)
                    │
                    ▼
         log-mel 特征 100 帧/秒 × 80 维
                    │
                    ▼
     降采样 8 倍 → 12.5 帧/秒 × 176 维
                    │
                    ▼
    12 层 Conformer（int8 + ESP32-S3 PIE SIMD）
                    │
                    ▼
        CTC 贪心解码 → "send help at grid four five"
                    │
                    ▼
           确认条 → TEXT_MESSAGE_APP → LoRa 广播
```

实测：2.60 秒语音用 2.59 秒 CPU 解完（RTF ≈ 0.99，单核）。

## 仓库结构

```
docs/                      设计文档（架构、分区、调试记录）
models/                    模型文件（见下）
patches/meshtastic-stt.patch   对 Meshtastic 源码的全部改动
scripts/
  flash_app.ps1            只烧 app（日常迭代用）
  flash_stt.ps1            烧 app + 文件系统 + 模型（首次烧录用）
  cap_log.ps1 / cap_stt.py 抓串口日志并汇总 STT 自检结果
  patch_mui.py             给 MUI 打"自家消息靠右显示"的补丁
```

> `research/meshtastic-firmware`（完整的 Meshtastic 克隆，约 1 GB）**没有**提交进仓库，
> 由 `patches/meshtastic-stt.patch` 还原。

## 快速开始

### 1. 准备 Meshtastic 源码

```powershell
git clone https://github.com/meshtastic/firmware.git
cd firmware
git checkout 51ac6ea7b7d7cb430acee10fcff41fbca94a20ba
git apply ..\patches\meshtastic-stt.patch
```

### 2. 编译（首次约 40~60 分钟）

```powershell
pio run -e seeed_wio_tracker_L2-tft
```

首次编译会下载依赖库。**编译完成后**再给 MUI 打补丁（补丁改的是 `.pio/libdeps` 里的库源码）：

```powershell
python ..\scripts\patch_mui.py
pio run -e seeed_wio_tracker_L2-tft
```

### 3. 烧录

首次（app + 文件系统 + 模型）：

```powershell
.\flash_stt.ps1 -Port COM83
```

之后只改了代码（app 分区）：

```powershell
.\flash_app.ps1 -Port COM83
```

模型分区布局（`default_16MB_stt.csv`）：

| 分区 | 偏移 | 大小 |
|---|---|---|
| app0 | `0x10000` | 4.75 MB |
| app1 | `0x4B0000` | 4.75 MB |
| model | `0x950000` | 5.75 MB |
| spiffs | `0xF10000` | 0.875 MB |
| coredump | `0xFF0000` | 64 KB |

### 4. 抓日志

```powershell
powershell -ExecutionPolicy Bypass -File .\cap_log.ps1 -Seconds 60 -Out stt_run.log
```

## 使用

1. 开机后等约 25 秒（模型加载 + 自检），屏幕右侧出现半透明 `MIC`
2. 按住 `MIC` 说话（最长 8 秒），按钮变红并显示秒数
3. 松手后约 2.5 秒，屏幕下方弹出确认条
4. 点 `SEND` 发送，点 `REDO` 重录

## 已知限制

- 仅英文，字符级 CTC 解码，**没有语言模型重排**，8 秒内错 2~4 个词属正常
- 单次最长 8 秒；解码耗时与语音时长 1:1，暂不支持流式
- 只能发主频道（`channels.getPrimaryIndex()`）
- 消息载荷上限 233 字节（Meshtastic 限制），超长会截断
- `patch_mui.py` 修改的是 `libdeps` 里的库，`pio pkg update` 或清理 `.pio` 后需要重新执行

## 许可与致谢

- 引擎：[Oído / tinyasr](https://github.com/lokutor-ai/oido)，GPL-3.0
- 模型：Oído v2（`oido_v2b.tnm`），见 `models/README.md`
- 宿主固件：[Meshtastic](https://github.com/meshtastic/firmware)，GPL-3.0
- 界面库：[meshtastic-device-ui](https://github.com/meshtastic/device-ui)

本项目同样以 **GPL-3.0** 发布，见 `LICENSE`。
