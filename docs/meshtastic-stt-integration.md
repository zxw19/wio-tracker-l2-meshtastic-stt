# Meshtastic 端侧语音转文字（STT）集成设计 — Wio Tracker L2 Pro

> 目标：在 **不替换任何现有功能** 的前提下，给 L2 Pro 的 Meshtastic 固件增加"说话 → 屏幕出字 → 确认后当普通文本消息发出"的能力。
> 引擎：**Oído**（`lokutor-ai/oido`，GPL-3.0，与 Meshtastic 同许可证）。语言：**仅英文**。
> 本文所有代码路径均相对于 `research/meshtastic-firmware`（Meshtastic `develop`，2026-11-04）与 `research/device-ui`。

---

## 0. 什么叫"融合而不是割裂"

割裂的做法是：起一个独立 FreeRTOS 任务，自己录音、自己识别、自己 `mesh->sendToMesh(...)` 把文本扔出去，屏幕上另画一个浮层。结果是一个寄生在固件里的独立 App —— 不遵守配置系统、不遵守输入框架、不遵守消息发送路径、和现有 UI 抢资源。

融合的做法是：**让语音成为现有五个扩展点里的第六个**。我在代码里找到的五个接缝，全部是现成的、有先例的：

| # | 现有接缝 | 证据 | 语音怎么接进去 |
|---|---|---|---|
| 1 | **输入驱动家族**（编译开关装配） | `device-ui/source/graphics/DeviceGUI.cpp:15-29, 45-55`：`INPUTDRIVER_ENCODER_TYPE` / `_MATRIX_TYPE` / `_BUTTON_TYPE` / `_DIRECTIONALPAD_TYPE` 四个同构分支 | 新增 `INPUTDRIVER_VOICE_TYPE`，成为第五个成员 |
| 2 | **输入策略管线** | `device-ui/include/input/policy/`：`IInputSource::poll(InputEvent&)` → `InputSourceRegistry` → `DefaultInputPolicyFactory` → `PolicyChain` → `InputPipeline` → `IUICommandDispatcher` | 语音实现 `IInputSource`，把识别结果当作 `InputEvent{action=Character, unicode=...}` 喂进管线 |
| 3 | **消息编辑器的文本注入先例** | `device-ui/source/graphics/TFT/TFTView_320x240.cpp:2756-2772` `ui_event_quick_chat_button()`：点击 = `lv_textarea_add_text(objects.message_input_area, quickMsg)`；长按 = 再补一个 `lv_obj_send_event(..., LV_EVENT_READY, nullptr)` 直接发送 | 麦克风按钮**照抄这套语义**，一行新协议都不用发明 |
| 4 | **音频硬件声明已就位** | `variants/esp32s3/seeed_wio_tracker_L2/variant.h`：`HAS_ES8311` + `DAC_I2S_BCK/WS/DOUT/MCLK`、`HAS_ES7243E` + `ADC_I2S_DIN 15`、`AUDIO_AMP_ENABLE(on)`、`AUDIO_AMP_SETTLE_MS 250` | 麦克风路径直接消费这些**已存在但当前无人使用**的宏 |
| 5 | **模块注册点** | `src/modules/Modules.cpp`：`#ifdef ARCH_ESP32` 下 `#if defined(USE_SX1280) && !MESHTASTIC_EXCLUDE_AUDIO` → `audioModule = new AudioModule();` | 并列加一个 `#if defined(MESHTASTIC_INCLUDE_STT)` → `speechToTextModule = new SpeechToTextModule();` |

**最强的融合证据**：语音消息最终以 `PortNum_TEXT_MESSAGE_APP` 发出，走 `controller->sendTextMessage(...)`（`TFTView_320x240.cpp:5980`）—— 和键盘打出来的消息**在协议层完全同一个东西**。网络上任何其他节点、手机 App、Web UI 都分辨不出这条消息是说的还是打的。**对端零改动。**

---

## 1. 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│ L5  device-ui 表现层（融合层）                                │
│     · VoiceInputDriver  : input_policy::IInputSource         │
│     · 麦克风按钮         : 复用 quick_chat 的点击/长按语义    │
│     · 流式增量文本       → lv_textarea_add_text(delta)        │
│     · 设置项 + locale/en.yml 文案                            │
├──────────────────────────────────────────────────────────────┤
│ L4  桥接：src/graphics/tftSetup.cpp                          │
│     DeviceScreen::create(spiLock) → init(new PacketClient)   │
│     语音服务在此注册回调（与 prepareSleep/wakeUp 观察者同构） │
├──────────────────────────────────────────────────────────────┤
│ L3  SpeechToTextModule（firmware，新增）                     │
│     · concurrency::OSThread，**不是** ProtobufModule<T>      │
│     · 状态机 IDLE/LISTENING/DECODING，暴露 Observer          │
│     · 任务钉在 **core 0**                                    │
│     · **自己不发包**——只产出文本，发不发由 UI 决定            │
├──────────────────────────────────────────────────────────────┤
│ L2  tinyasr（Oído 引擎，移植为 PlatformIO library）          │
│     · 纯 C99，无 IDF 组件依赖 → 移植成本极低                 │
│     · tasr_alloc/tasr_free 钩子接到 heap_caps_*              │
│     · 模型经 esp_partition_mmap 就地 XIP，永不整体进 RAM      │
├──────────────────────────────────────────────────────────────┤
│ L1  L2AudioInput（新增，唯一的新硬件代码）                   │
│     · ES7243E I2C 初始化 + I2S RX @ 16 kHz mono PCM16        │
│     · 你手里小智/L2 那套可直接复用                           │
└──────────────────────────────────────────────────────────────┘
```

**关键设计决定：L3 不发包。** 这是"融合"最重要的一条。语音模块是一个**输入设备**，不是一个**消息来源**。它把文本交给 L5，由 L5 走既有发送路径。这样：
- 用户在屏幕上看到文字、可以改、可以删、可以取消 → 满足你要的"屏幕确认"
- 发送逻辑、ACK/重传、频道选择、加密、hop limit 全部复用，一行不写
- 关掉语音功能，固件行为与今天完全一致

---

## 2. 分区表 —— 本项目的头号硬约束

L2 用的是固件根目录的 `default_16MB.csv`（见 `variants/esp32s3/seeed_wio_tracker_L2/platformio.ini` 的 `board_build.partitions`）：

```
nvs,      data, nvs,     0x9000,  0x5000,     # 20 KB
otadata,  data, ota,     0xE000,  0x2000,     # 8 KB
app0,     app,  ota_0,   0x10000, 0x640000,   # 6.25 MB
app1,     app,  ota_1,   0x650000,0x640000,   # 6.25 MB
spiffs,   data, spiffs,  0xC90000,0x360000,   # 3.375 MB
coredump, data, coredump,0xFF0000,0x10000,    # 64 KB
```

**双 OTA 吃掉了 12.5 MB，只剩 3.375 MB spiffs —— 装不下模型。**

模型体积（英文，Oído v2，2026-11-03 发布，单核实时）：
- `oido_v2b.tnm` = **5.9 MB**（流式，RTF 0.54，说完 259 ms 出字）← **推荐**
- `oido_v2.tlm` = **1.3 MB**（beam search 语言模型，可选）
- 合计 **7.2 MB**；Oído 分区按十进制 MB 取整，实际分配 `0x5C0000`(6.03MB) + `0x150000`(1.31MB) = **7.4 MB**

### 三个方案（偏移已逐条验算，可直接用）

**方案 A — 保留双 OTA，app 槽缩到 3.5 MB（产品化首选）**
```
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xE000,  0x2000,
app0,     app,  ota_0,   0x10000, 0x380000,   # 3.5 MB
app1,     app,  ota_1,   0x390000,0x380000,   # 3.5 MB
model,    data, 0x40,    0x710000,0x5C0000,   # oido_v2b.tnm
lm,       data, 0x42,    0xCD0000,0x150000,   # oido_v2.tlm
spiffs,   data, spiffs,  0xE20000,0x1D0000,   # 1.81 MB
coredump, data, coredump,0xFF0000,0x10000,
```
成立条件：**L2-tft 固件 .bin ≤ 3.5 MB**（必须实测，见 Phase 0）。

**方案 B — 放弃 OTA，单个 6 MB factory app（最稳，牺牲空中升级）**
```
nvs,      data, nvs,     0x9000,  0x6000,
factory,  app,  factory, 0x10000, 0x600000,   # 6 MB
model,    data, 0x40,    0x610000,0x5C0000,
lm,       data, 0x42,    0xBD0000,0x150000,
spiffs,   data, spiffs,  0xD20000,0x2C0000,   # 2.75 MB
coredump, data, coredump,0xFE0000,0x10000,
```

**方案 C — 不带语言模型（贪心 CTC 解码），保住 4 MB 双 OTA**
```
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xE000,  0x2000,
app0,     app,  ota_0,   0x10000, 0x400000,   # 4 MB
app1,     app,  ota_1,   0x410000,0x400000,   # 4 MB
model,    data, 0x40,    0x810000,0x5C0000,
spiffs,   data, spiffs,  0xDD0000,0x210000,   # 2.06 MB
coredump, data, coredump,0xFE0000,0x10000,
```
代价：WER 上升（Oído 报的 LibriSpeech test-clean 从带 LM 的水平退化到贪心解码）。**但作为 Phase 2 的第一块里程碑，它最简单，建议先跑通它。**

分区子类型 `0x40`(model) / `0x42`(lm) 沿用 Oído `esp32/firmware/partitions*.csv` 的约定，`tasr` 侧按名字 `"model"` / `"lm"` 查找即可。

**待核实**：spiffs 从 3.375 MB 缩到 1.81 MB 是否安全。上游 #12249 "MUI: adapt to device-ui storage (FATFS) and SD bus classes" 暗示存储已迁到 SD/FATFS，但 `device-ui/maps`、`device-ui/resources` 是否落在 spiffs 需要查一遍构建产物。

### SD 卡的角色（对齐你之前的直觉）
SD 卡**不能**作为运行时模型载体：L2 只有 1-bit SDIO（`variant.h`/`platformio.ini` 里只有 `SD_MOSI_PIN=3`(CMD)、`SD_SCLK_PIN=2`、`SD_MISO_PIN=1`(D0)），实测 2–4 MB/s，而 `tasr_model_load()` 要求 blob 全程有效（就地 XIP），SD 无法 mmap。

所以 SD 的正确定位是**分发介质**，和你说的离线地图一模一样：
- 出厂/下载把 `.tnm`+`.tlm` 放到 SD 卡
- 首次开机检测 flash `model` 分区为空 → 从 SD 拷进去 → 之后一直 XIP
- **好处（对 marketing 极有价值）**：可以只发布一个固件镜像，语音作为可选下载包。不装模型 = 功能和今天完全一样；装上 = 多出语音。

---

## 3. 内存与核心预算

| 资源 | Oído 需求（裸 IDF 实测） | L2 Pro 供给 | 结论 |
|---|---|---|---|
| Flash | 5.9 MB 模型 (+1.3 MB LM) | 16 MB | 见第 2 节，够 |
| PSRAM | 20 s 语音峰值 2.4 MB；流式缓存 0.3–0.4 MB | **8 MB octal (OPI)** | 宽松 |
| 内部 SRAM | ≥122 KB 空闲（开流式缓存 72 KB） | 总共 320 KB（board JSON `maximum_ram_size: 327680`） | **头号风险** |

**PSRAM 类型已确认**：`boards/seeed_wio_tracker_L2.json` → `"psram_type": "opi"`、`"memory_type": "qio_opi"`、`"name": "seeed_wio_tracker_L2 (16 MB FLASH, 8 MB PSRAM)"`。Oído 的硬要求（README Limitations）是 "ESP32-S3 with 16 MB flash and **8 MB octal PSRAM (N16R8)**"，其 `sdkconfig.defaults` 写 `CONFIG_SPIRAM_MODE_OCT=y`。**精确对上，之前 octal/quad 的疑问关闭。**

**内部 SRAM 缓解手段（Oído 已内建钩子，`esp32/components/tinyasr/include/tinyasr.h`）**：
- `tasr_alloc(size, kind)` / `tasr_free(p)`：`kind 0` = 大块慢速可接受 → `heap_caps_malloc(size, MALLOC_CAP_SPIRAM)`；`kind 1` = 热数据 → `MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT`。**把一切能推的都推到 kind 0。**
- `tasr_model_place_weights(m, budget)`：把权重矩阵搬进 kind-0 内存，上限 `budget` 字节 —— 这是 SRAM↔速度的调节旋钮。
- `TASR_NO_SIMD`：编掉 PIE SIMD 内核，省 IRAM、变慢。最后的退路。

**核心分配（重要）**：
- Oído 的**双核模式在真实芯片上是坏的** —— 240 MHz 下转写结果随机错乱，约 1/8 概率 `IllegalInstruction` 崩溃；只有降到 160 MHz 或 `TASR_NO_SIMD` 才正确。上游已默认单核。→ **保持 `tasr_parallel` 默认串行实现，不要碰 `tasr_par_mask`。**
- L2 board JSON 设了 `ARDUINO_RUNNING_CORE=1`、`ARDUINO_EVENT_RUNNING_CORE=1` → Arduino loop 与绝大多数 OSThread 在 core 1。
- → **ASR 任务用 `xTaskCreatePinnedToCore(..., /*core=*/0)` 钉在 core 0**，与 mesh/LoRa 任务物理隔离。这直接服务于你"别搞坏现有功能"的底线。

**必须走流式，不能走整段接口**：README Limitations 原文 —— "the one-shot batch `transcribe` call inside a live microphone capture loop **hangs on the chip** … use streaming for live capture." v2b 本身就是流式训练的，用 `tasr_stream_feed()` / `tasr_stream_finish()` / `tasr_stream_text()` 即可。

---

## 4. 唯一的真实硬件冲突：I2S 时钟域

L2 的 ES8311（DAC/喇叭）和 ES7243E（ADC/麦克风）**共用同一组时钟线**：

| | BCK | WS | MCLK | 数据 |
|---|---|---|---|---|
| ES8311 DAC | GPIO 11 | GPIO 12 | GPIO 10 | `DAC_I2S_DOUT` = GPIO 16 |
| ES7243E ADC | GPIO 11 | GPIO 12 | GPIO 10 | `ADC_I2S_DIN` = GPIO 15 |

而 `src/AudioThread.h` 里 `initOutput()` 已经用 ESP8266Audio 占住了输出：
```cpp
audioOut = std::unique_ptr<AudioOutputI2S>(new AudioOutputI2S(1, AudioOutputI2S::EXTERNAL_I2S));
audioOut->SetPinout(DAC_I2S_BCK, DAC_I2S_WS, DAC_I2S_DOUT, DAC_I2S_MCLK);
```

**不能**再开第二个 I2S 外设去驱动同样的 BCK/WS —— 时钟会打架。三条路：

- **A（Phase 1 采用，最小侵入）**：STT 是按键触发的瞬时行为，与 RTTTL/SAM 播报天然互斥。用 ESP-IDF v5 `driver/i2s_std.h` 在 I2S_NUM_1 上开 **RX-only** 通道，由它作为 master 产生 BCK/WS/MCLK；并加一个互斥保证 `AudioThread::beginRttl()/readAloud()` 期间不启动采集（反之亦然）。功放使能走现成的 `AUDIO_AMP_ENABLE(on)` 宏。**不改 `AudioThread.h` 的现有行为。**
- **B（长期正确）**：抽一个 `L2AudioBus`，持有一对 full-duplex `i2s_chan_handle_t`（TX+RX 同时钟域），`AudioThread` 与 STT 采集都从它取通道。架构最干净，但要动 `AudioThread.h`，Phase 5 再考虑。
- **C（不要用）**：`src/modules/esp32/AudioModule.cpp` 那套。它在 `AudioModule.cpp:154` 硬写 `sample_rate = 8000`、绑定 Codec2、用已废弃的 `i2s_driver_install`；而且 `AudioModule.h` 的守卫是 `#if defined(ARCH_ESP32) && defined(USE_SX1280)` —— **L2 是 SX1262，这个模块在 L2 上根本不参与编译**。只能当写法参考，不能复用。

Oído 需要 **16 kHz 单声道 PCM16**（`tasr_stream_feed` 的入参约定）。

---

## 5. 配置管道：两阶段，先不碰 protobufs

`protobufs/` 是**独立子模块**（`meshtastic/protobufs`），改它意味着一个单独 PR + bump submodule，是整条上游链路里最慢的一环。所以分两步：

**Phase 1–4（不动 protobufs）**：用编译开关 + `userPrefs.jsonc`（固件根目录已存在）。
- `-D MESHTASTIC_INCLUDE_STT`（固件侧模块开关，对齐既有 `MESHTASTIC_EXCLUDE_AUDIO` 命名风格）
- `-D INPUTDRIVER_VOICE_TYPE=1`（device-ui 侧，对齐既有四个 `INPUTDRIVER_*_TYPE`）
- 两个开关都只写进 `variants/esp32s3/seeed_wio_tracker_L2/platformio.ini` 的 `-tft` env → **对其他 100+ 个 board 零影响**

**Phase 5（上游化时再做）**：`protobufs/meshtastic/module_config.proto` 的 `AudioConfig` 当前占用字段 1–7：
```
bool      codec2_enabled = 1;
uint32    ptt_pin        = 2;
Audio_Baud bitrate       = 3;
uint32    i2s_ws         = 4;
uint32    i2s_sd         = 5;
uint32    i2s_din        = 6;
uint32    i2s_sck        = 7;
```
顺延新增（不改任何既有字段号，向后兼容）：
```proto
bool   stt_enabled           = 8;   // 默认 false：不装模型的设备行为与今天完全一致
bool   stt_auto_send         = 9;   // false = 屏幕确认后手动发送（默认）
uint32 stt_end_of_speech_ms  = 10;  // VAD 尾静音，Oído 默认 800 ms
bool   stt_use_lm            = 11;  // beam search + .tlm；false = 贪心 CTC
uint32 stt_sram_budget_kb    = 12;  // 映射到 tasr_model_place_weights() 的预算
```

> 注意：仓库 `AGENTS.md` 建议"新模块继承 `src/mesh/ProtobufModule.h` 的 `ProtobufModule<T>`"。**这里刻意不这么做** —— `ProtobufModule<T>` 是给"收发自定义 protobuf 载荷"的模块用的，而 STT 的输出是普通文本、走 `TEXT_MESSAGE_APP`，没有自己的 wire format。强行套 `ProtobufModule<T>` 反而会造成割裂。正确基类是 `concurrency::OSThread`（+ 需要 UI 帧时再混入 `Observable<const UIFrameEvent *>`，参照 `AudioModule.h` 的类声明形状）。这个偏离要在 PR 描述里写明理由。

---

## 6. 分阶段计划（每阶段带一个可失败的关卡）

| Phase | 内容 | 工期 | 关卡（不过就别往下走） |
|---|---|---|---|
| **0** | 构建原版 `pio run -e seeed_wio_tracker_L2-tft`，记录 `.bin` 体积、`ESP.getFreeHeap()`、`ESP.getFreePsram()` | 0.5 天 | .bin ≤3.5MB → 方案 A；≤4MB → 方案 C；否则方案 B |
| **1** | **麦克风 bring-up**：ES7243E + I2S RX 16k，录 5 秒存成 SD 卡上的 WAV | 2–3 天 | 把 WAV 拿到电脑上 `esp32/host/tasr_cli model.wav out.wav` 转写，**英文识别得出来** |
| **2** | `tinyasr` 移植成 PlatformIO library + `model` 分区 + `esp_partition_mmap` 加载 + 从 SD 读 WAV 离线识别 | 3–5 天 | 板上实测 RTF < 1.0、内部 SRAM 不 OOM |
| **3** | 实时麦克风 → 流式识别 → 回调，任务钉 core 0 | 3–5 天 | **识别的同时 LoRa 照常收发**（这是"别搞坏现有功能"的验收关） |
| **4** | device-ui 融合：`VoiceInputDriver` + 麦克风按钮 + 增量文本进 `message_input_area` + 设置开关 | 4–6 天 | 说完话，文字出现在**原有的**输入框里，可改可删，按**原有的**发送键发出 |
| **5** | 打磨：ES8311 提示音、状态栏指示、`locale/en.yml` 文案、`trunk fmt`、`./bin/run-tests.sh`、protobuf 配置项上游化 | 2–3 天 | 上游 PR 形态成立 |

合计 **约 3–4 周**到可演示。

**Phase 1 是整个项目的去风险关键**：它把"硬件能不能采到合格音频"和"ASR 准不准"这两件事，从固件集成里彻底剥离出来。WAV 在电脑上转写得好，剩下的就纯粹是管道工程；转写得差，再多的集成也是白搭。

**Phase 0 之前还有一件零成本的事**：先跑 Oído 的 WASM 浏览器 demo（`lokutor-ai.github.io/oido/demo/`）—— 同一份 C 引擎 + 同一个 int4 模型编译成 WebAssembly，麦克风直采、数据不出设备。**先用它验证你的英文口音识别率**，再决定要不要投这 3–4 周。

---

## 7. 代码骨架（融合接缝处，可直接落地）

### 7.1 L2 —— `tinyasr` 移植为 PlatformIO library

`esp32/components/tinyasr/CMakeLists.txt` 只有 5 个源文件、无 IDF 组件依赖、编译选项 `-O2 -ffp-contract=off`：
```cmake
idf_component_register(SRCS "tinyasr.c" "kernels.c" "tinyasr_lm.c" "tasr_nemo.c" "tasr_seg.c"
                       INCLUDE_DIRS "include" "." PRIV_INCLUDE_DIRS ".")
target_compile_options(${COMPONENT_LIB} PRIVATE -O2 -ffp-contract=off -Wno-unused-function)
```
→ 平移到 `lib/tinyasr/library.json`：
```json
{
  "name": "tinyasr",
  "version": "2.0.0",
  "description": "Oido streaming ASR engine (GPL-3.0), ported from ESP-IDF component",
  "build": {
    "srcDir": "src",
    "flags": ["-Iinclude", "-O2", "-ffp-contract=off", "-Wno-unused-function"]
  }
}
```
`-ffp-contract=off` **必须保留** —— 它阻止编译器把乘加收缩成 FMA，是 Oído 板上/模拟器/host 三方结果 bit 级一致的前提。

源文件照搬 `tinyasr.c`、`kernels.c`、`tinyasr_lm.c`、`tasr_nemo.c`、`tasr_seg.c` + `include/` 下的 `tinyasr.h`、`tasr_nemo.h`、`tasr_seg.h`、`kernels.h`、`tasr_math.h`。

内存钩子实现（放在 L3 模块里，引擎侧只是 `extern` 函数指针）：
```cpp
static void *sttAlloc(size_t size, int kind)
{
    // kind 0 = 大块/慢速可接受 → octal PSRAM；kind 1 = 热数据 → 内部 SRAM
    uint32_t caps = (kind == 0) ? (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
                                : (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    void *p = heap_caps_malloc(size, caps);
    if (!p && kind == 1) // SRAM 不足时降级到 PSRAM，宁可慢也不要崩
        p = heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return p;
}
static void sttFree(void *p) { heap_caps_free(p); }

// init 时挂上：
tasr_alloc = sttAlloc;
tasr_free  = sttFree;
// tasr_parallel 保持默认串行实现 —— 双核模式在 240MHz 真实芯片上会产生错误转写
```

模型加载（就地 XIP，永不整体进 RAM）：
```cpp
static const esp_partition_t *findData(const char *label)
{
    return esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, label);
}

bool SpeechToTextModule::loadModel()
{
    const esp_partition_t *mp = findData("model");
    if (!mp) { LOG_ERROR("STT: no 'model' partition"); return false; }

    const void *blob = nullptr;
    esp_partition_mmap_handle_t h;
    if (esp_partition_mmap(mp, 0, mp->size, ESP_PARTITION_MMAP_DATA, &blob, &h) != ESP_OK) {
        LOG_ERROR("STT: mmap model failed"); return false;
    }
    model = tasr_model_load(static_cast<const uint8_t *>(blob), mp->size); // blob 必须全程有效
    if (!model) { LOG_ERROR("STT: tasr_model_load failed"); return false; }

    // 按剩余内部 SRAM 决定把多少权重搬去 PSRAM —— Phase 0 的实测值填进来
    tasr_model_place_weights(model, sttSramBudgetBytes);

    if (moduleConfig.audio.stt_use_lm) {
        const esp_partition_t *lp = findData("lm");
        // … 同样 mmap，tasr_lm_load()，再 tasr_stream_set_decoder(stream, dec)
    }
    return true;
}
```

### 7.2 L3 —— `src/modules/esp32/SpeechToTextModule.h`

```cpp
#pragma once
#include "Observer.h"
#include "concurrency/OSThread.h"
#include "configuration.h"

#if defined(ARCH_ESP32) && defined(MESHTASTIC_INCLUDE_STT) && !MESHTASTIC_EXCLUDE_AUDIO
#include <Arduino.h>

extern "C" {
#include "tinyasr.h"
}

// 语音模块是一个「输入设备」，不是「消息来源」。
// 它只产出文本；发不发消息由 UI 层走既有的 sendTextMessage 路径决定。
struct SttEvent {
    enum Kind { kPartial, kFinal, kError, kStateChanged } kind;
    char text[meshtastic_Constants_DATA_PAYLOAD_LEN]; // 与文本消息同上限，天然防溢出
    bool listening;
};

class SpeechToTextModule : public concurrency::OSThread, public Observable<const SttEvent *>
{
  public:
    SpeechToTextModule();

    void startListening();   // UI 按下麦克风
    void stopListening();    // UI 松开 / VAD 判定说完
    bool isListening() const { return state == State::kListening; }
    bool isReady() const { return model != nullptr; }

  protected:
    int32_t runOnce() override;   // 只负责状态机推进；重活在 core 0 的采集任务里

  private:
    enum class State { kIdle, kListening, kDecoding };
    static void captureTask(void *arg);   // xTaskCreatePinnedToCore(..., 0)

    bool loadModel();
    void publish(SttEvent::Kind kind, const char *text);

    tasr_model_t *model = nullptr;
    tasr_stream_t *stream = nullptr;
    volatile State state = State::kIdle;
    TaskHandle_t captureHandle = nullptr;
};

extern SpeechToTextModule *speechToTextModule;
#endif
```

采集任务的核心循环（**流式，不用整段接口** —— 整段接口在实时麦克风循环里会在芯片上挂死）：
```cpp
void SpeechToTextModule::captureTask(void *arg)
{
    auto *self = static_cast<SpeechToTextModule *>(arg);
    self->stream = tasr_stream_create(self->model, /*chunk=*/32, /*left_chunks=*/4);
    // chunk=32 帧 @25Hz ≈ 1.28 s 一个步长；调小可降延迟、升开销

    static int16_t pcm[512];
    while (self->state != State::kIdle) {
        int n = l2AudioInputRead(pcm, 512);      // L1：阻塞读 16kHz mono PCM16
        if (n <= 0) continue;

        int newTokens = tasr_stream_feed(self->stream, pcm, n);
        if (newTokens > 0)
            self->publish(SttEvent::kPartial, tasr_stream_text(self->stream));

        if (l2AudioInputEndOfSpeech(800)) {     // 800ms 尾静音，对齐 Oído 默认
            tasr_stream_finish(self->stream);
            self->publish(SttEvent::kFinal, tasr_stream_text(self->stream));
            break;
        }
    }
    tasr_stream_reset(self->stream);
    vTaskDelete(nullptr);
}
```

注册点（`src/modules/Modules.cpp`，与 `audioModule` 并列）：
```cpp
#ifdef ARCH_ESP32
    // Only run on an esp32 based device.
#if defined(USE_SX1280) && !MESHTASTIC_EXCLUDE_AUDIO
    audioModule = new AudioModule();
#endif
#if defined(MESHTASTIC_INCLUDE_STT) && !MESHTASTIC_EXCLUDE_AUDIO
    if (moduleConfig.audio.stt_enabled)          // Phase 5 之前先用 userPrefs 常量
        speechToTextModule = new SpeechToTextModule();
#endif
```

### 7.3 L1 —— `src/platform/esp32/L2AudioInput.h`

唯一的新硬件代码，直接消费 `variant.h` 里**已存在但当前无人使用**的宏：
```cpp
#pragma once
#include "configuration.h"
#if defined(HAS_ES7243E) && defined(MESHTASTIC_INCLUDE_STT)
#include <stddef.h>
#include <stdint.h>

#define STT_SAMPLE_RATE_HZ 16000   // tinyasr 固定要求 16kHz mono PCM16

bool l2AudioInputBegin(void);      // ES7243E I2C 初始化 + I2S RX（用 ADC_I2S_BCK/WS/DIN/MCLK）
void l2AudioInputEnd(void);
int  l2AudioInputRead(int16_t *buf, int maxSamples);   // 返回实际读到的样本数
bool l2AudioInputEndOfSpeech(uint32_t tailSilenceMs);  // 简单能量 VAD
#endif
```
> 实现要点见第 4 节：BCK/WS/MCLK 与 ES8311 共用，必须由**一个** master 产生，且与 `AudioThread` 的播报互斥。你手上小智/L2 的音频初始化代码可直接搬进来。

### 7.4 L5 —— `device-ui/include/input/VoiceInputDriver.h`

严格对齐 `KeyMatrixInputDriver.h` 的形状，成为输入驱动家族的第五个成员：
```cpp
#pragma once
#include "input/InputDriver.h"
#include "input/policy/IInputSource.h"
#include <deque>

// 把 STT 结果包装成标准 InputEvent，走与物理键盘完全相同的策略管线。
class VoiceInputDriver : public InputDriver, public input_policy::IInputSource
{
  public:
    VoiceInputDriver(void);
    virtual void init(void) override;
    virtual ~VoiceInputDriver(void) {}

    // input_policy::IInputSource
    virtual const char *getSourceId() const override { return "voice"; }
    virtual input_policy::InputCapabilities getCapabilities() const override;
    virtual bool poll(input_policy::InputEvent &outEvent) override;

    // 由固件侧 SpeechToTextModule 的 Observer 回调调用（线程安全，仅入队）
    void enqueueText(const char *text);

  private:
    static void keyboard_read(lv_indev_t *indev, lv_indev_data_t *data);
    std::deque<uint32_t> pendingUnicode;   // 待注入的码点队列
};
```

`init()` 里的管线装配，**逐字照抄** `KeyMatrixInputDriver.cpp:88-105` 的既有惯用法：
```cpp
if (!contextProvider) {
    contextProvider = std::shared_ptr<input_policy::IInputContextProvider>(
        &input_policy::InputContextState::instance(),
        [](input_policy::IInputContextProvider *) {});
}
if (!commandDispatcher) {
    commandDispatcher = std::shared_ptr<input_policy::IUICommandDispatcher>(
        &input_policy::UICommandDispatcher::instance(),
        [](input_policy::IUICommandDispatcher *) {});
}
if (!voicePipeline) {
    input_policy::InputSourceRegistry registry;
    input_policy::DefaultInputPolicyFactory factory;
    auto result = factory.build(registry, contextProvider, commandDispatcher);
    voicePipeline = std::make_shared<input_policy::InputPipeline>(result.bindingResolver,
                                                                 contextProvider, commandDispatcher);
    voicePipeline->setPolicyChain(std::move(result.chain));
}
```

`getCapabilities()`：`supportsTextEntry = true`、`supportsLongPress = true`，其余全 false —— 让 `DefaultInputPolicyFactory` 自动组出正确的策略链，不需要为语音写任何特例策略。

`DeviceGUI.cpp` 的装配点（与既有四个分支同构）：
```cpp
#if defined(INPUTDRIVER_VOICE_TYPE)
#include "input/VoiceInputDriver.h"
static VoiceInputDriver *voiceDriver = nullptr;
#endif
...
#if defined(INPUTDRIVER_VOICE_TYPE)
    voiceDriver = new VoiceInputDriver;
#endif
```

### 7.5 L5 —— 麦克风按钮：照抄 QuickChat 的交互契约

`TFTView_320x240.cpp:2756-2772` 已经定义好了一套用户学过的语义，**直接复用，不发明新交互**：
- **点击** = 文本进输入框，**不发送**，等用户确认
- **长按** = 文本进输入框 **并**触发 `LV_EVENT_READY` 直接发送

```cpp
void TFTView_320x240::ui_event_mic_button(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);

    if (code == LV_EVENT_CLICKED) {
        if (speechToTextModule && speechToTextModule->isReady()) {
            speechToTextModule->startListening();
            lv_obj_add_state(objects.mic_button, LV_STATE_CHECKED);   // 录音中视觉反馈
            lv_textarea_set_placeholder_text(objects.message_input_area, _("Listening ..."));
        }
        return;
    }

    if (code == LV_EVENT_LONG_PRESSED) {
        pendingAutoSend = true;    // 识别完成后自动发送，与 quick_chat 长按语义一致
        if (speechToTextModule) speechToTextModule->startListening();
    }
}

// 由 SpeechToTextModule 的 Observer 回调驱动（LVGL 线程内执行）
void TFTView_320x240::onSttEvent(const SttEvent &evt)
{
    if (evt.kind == SttEvent::kFinal) {
        lv_obj_remove_state(objects.mic_button, LV_STATE_CHECKED);
        lv_textarea_set_placeholder_text(objects.message_input_area, _("Enter Text ..."));

        if (evt.text[0]) {
            // 与 ui_event_quick_chat_button:2764 完全相同的注入方式
            lv_textarea_add_text(objects.message_input_area, evt.text);
            lv_group_focus_obj(objects.message_input_area);

            if (pendingAutoSend) {   // 对齐 quick_chat 长按：注入 + 触发 READY
                lv_obj_send_event(objects.message_input_area, LV_EVENT_READY, nullptr);
                pendingAutoSend = false;
            }
        }
    }
}
```

**流式增量显示**（可选，但是 demo 的高光时刻）：`tasr_stream_text()` 每次返回**到目前为止的完整文本**，所以只提交新增后缀即可：
```cpp
size_t cur = strlen(evt.text);
if (cur > committedLen && memcmp(evt.text, committed, committedLen) == 0) {
    lv_textarea_add_text(objects.message_input_area, evt.text + committedLen);  // 仅追加增量
} else {
    lv_textarea_set_text(objects.message_input_area, evt.text);  // 重打分导致前缀变化，整体重绘
}
committedLen = cur; memcpy(committed, evt.text, cur + 1);
```

文案走既有 i18n：`_("Listening ...")`，字符串加进 `device-ui/locale/en.yml`（其余语种自动回退）。

---

## 8. 需要改动的文件清单

**meshtastic-firmware（新增 4 个 / 修改 3 个）**
```
新增  lib/tinyasr/                                     (Oído 引擎移植)
新增  src/platform/esp32/L2AudioInput.{h,cpp}          (ES7243E + I2S RX 16k)
新增  src/modules/esp32/SpeechToTextModule.{h,cpp}     (状态机 + core 0 任务)
新增  default_16MB_stt.csv                             (第 2 节方案 A/B/C 之一)
改    src/modules/Modules.cpp                          (注册点，与 audioModule 并列)
改    src/graphics/tftSetup.cpp                        (把 STT Observer 接到 DeviceScreen)
改    variants/esp32s3/seeed_wio_tracker_L2/platformio.ini
                                                       (-tft env 加两个开关 + 换分区表)
```

**device-ui（新增 2 个 / 修改 3 个）**
```
新增  include/input/VoiceInputDriver.h
新增  source/input/VoiceInputDriver.cpp
改    source/graphics/DeviceGUI.cpp                    (INPUTDRIVER_VOICE_TYPE 分支)
改    source/graphics/TFT/TFTView_320x240.cpp          (麦克风按钮 + onSttEvent)
改    locale/en.yml                                    (文案)
```

**上游化时再加（Phase 5）**
```
改    protobufs/meshtastic/module_config.proto         (AudioConfig 字段 8–12，独立 PR)
改    protobufs/meshtastic/module_config.options       (如需)
```

**改动半径**：全部新增代码都在 `#if defined(MESHTASTIC_INCLUDE_STT)` / `#if defined(INPUTDRIVER_VOICE_TYPE)` 之内，两个开关只写在 L2 的 `-tft` env 里 → **对其余 100+ 个 board、对不开这个功能的 L2，编译产物逐字节不变。** 这是"不割裂、不破坏"的机械保证。

---

## 9. 待你确认 / 待实测的开放项

1. **L2 Pro 与上游 variant 的对应关系**。Meshtastic 上游只有 `seeed_wio_tracker_L2`（`hw_model = 137`，slug `SEEED_WIO_TRACKER_L2`），**没有单独的 L2 Pro variant**。规格看完全一致（320×240 NV3031B QSPI 触屏、ES8311+ES7243E、L76K、SD SDIO 1-bit、SX1262、16MB/8MB OPI PSRAM），但 L1 那边是有 `L1` / `L1_Pro_1W` / `L1_eink` 三个独立 variant 的。→ **你手上 L2 Pro 的固件源码，是直接跟上游这个 variant 对齐，还是 Seeed 内部另有一份 fork？** 这决定了是提 PR 到上游，还是在内部 fork 上加模块。
2. **Phase 0 的三个实测数**：`.bin` 体积、空闲内部 heap、空闲 PSRAM。决定分区方案 A/B/C。
3. **spiffs 缩到 1.81 MB 是否安全**（`device-ui/maps`、`device-ui/resources` 落在哪个分区）。
4. **arduino-esp32 捆绑的 IDF 版本**。Oído 的板级数据是 ESP-IDF **5.5.1** 上测的，v2 引擎依赖 "the ROM's asynchronous data-cache preload"。若捆绑 IDF 偏旧，是给 `tinyasr` 打补丁的活，不是阻断项（它的 CMakeLists 无 IDF 组件依赖）。
5. **模型权重的许可证**：`oido_v2b.tnm` / `oido_v2.tlm` 是 **CC-BY-SA-4.0**（含 share-alike）。代码侧 GPL-3.0 与 Meshtastic 完全兼容没问题，但**如果这个要作为 Seeed 出厂预装的产品功能，SA 条款对模型权重的约束需要法务过一眼**。若不行，退路是 `nemo8.tnm`（CC-BY-4.0，14.0 MB，但只有整段模式、说完约 3 s 出字），或改用 **BabyTalk**（代码 MIT + 模型 Citrinet-256 CC-BY-4.0，6.0 MB flash / 84.5 KB 内部 RAM，代价是只能整段转写、要吃两个核）。

---

## 10. 线程与核心地图（实测代码得出，修正 §3 的核心分配）

> §3 里"ASR 钉 core 0，与 mesh/LoRa 物理隔离"结论正确，但**不完整**：core 0 上已经住着 LVGL。以下是从代码里读出的真实地图。

| 核 | 任务 | 优先级 | 栈 | 证据 |
|---|---|---|---|---|
| **core 1** | Arduino `loopTask` → Meshtastic `loop()`、meshService、NodeDB、绝大多数 `concurrency::OSThread` | 1 | — | `boards/seeed_wio_tracker_L2.json`：`-D ARDUINO_RUNNING_CORE=1`、`-D ARDUINO_EVENT_RUNNING_CORE=1` |
| **core 0** | `tft` 任务 → `tft_task_handler()` 死循环调 `deviceScreen->task_handler()` + `sleep()` | **1** | **16384** | `src/graphics/tftSetup.cpp:432` `xTaskCreatePinnedToCore(tft_task_handler, "tft", TFT_TASK_STACK_SIZE, NULL, 1, NULL, 0)`；`TFT_TASK_STACK_SIZE` 定义在 `tftSetup.cpp:230` |
| **core 0**（新增） | `stt` 任务 → 麦克风采集 + `tasr_stream_feed` | **1**（见下） | 8–16 KB（需实测） | 本设计 |

**为什么 ASR 放 core 0 是对的**：core 1 上跑着 Arduino loop 与整条 mesh/LoRa 链路。ASR 是长时间占用 CPU 的计算任务（v2b RTF 0.54 = 每秒音频吃掉 0.54 秒 CPU）。放到 core 1 会直接与 LoRa 收发时序抢 CPU；放到 core 0 只与 UI 渲染抢，**mesh 侧零影响** —— 这正是"别搞坏现有功能"的机械保证。

**优先级取 1（与 `tft` 相同），不要更高**：
- 若 ASR 优先级 > 1，它一旦 runnable 就会饿死 LVGL → 说话期间界面完全冻结。
- 取相同优先级 1，靠 FreeRTOS 时间片轮转（Oído 的 `sdkconfig.defaults` 设 `CONFIG_FREERTOS_HZ=1000`）公平分享。
- 关键：**ASR 循环天然会让出 CPU** —— 每读一个音频块都要阻塞在 I2S DMA 上（`l2AudioInputRead`），这段时间 core 0 完整交给 LVGL。所以两者是自然交错的，不是硬抢。
- 余量核算：LVGL 刷一段文字 + 简单动画约占 core 0 的 5–15%，ASR 实得 ~85–95% → 有效 RTF ≈ 0.54 / 0.9 ≈ **0.6，仍 < 1.0，实时性保持**。若实测延迟漂移，再提到 2。

**跨线程 UI 访问：绝对不要在 ASR 任务里碰 LVGL 对象。**
- Meshtastic 的 `Observable<T>::notifyObservers()`（`src/Observer.h:69`）是**同步**的 —— 回调在**调用 notify 的那个线程**里执行。所以 `SpeechToTextModule`（core 0 的 stt 任务）一旦 `notifyObservers`，回调就在 stt 任务上下文里跑；此时若直接 `lv_textarea_add_text()`，就是在非 LVGL 线程操作 LVGL → 随机崩溃/花屏。
- **正确做法（仓库内已有先例）**：`device-ui/include/util/AsyncMessageLog.h` 就是这个模式 —— `Mutex`（ESP32 上 `xSemaphoreCreateMutexStatic`，其他平台 `std::mutex`）+ `std::list<Pending>` + `std::atomic<bool> running/exited` + 独立 `workerTask`。**照它的形状做一个 `SttEventQueue`**：stt 任务只入队，`DeviceGUI::task_handler()`（即 `tft` 任务、LVGL 线程）出队并操作控件。
- 附带好处：`tft` 与 `stt` **同在 core 0**，队列交接是核内的，没有跨核缓存竞争。
- `DeviceScreen.cpp:46` 的 `xSemaphore` 只用于 sleep/wake 过渡（`prepareSleep`/`wakeUp`），不是通用 LVGL 锁，不要复用它。

**看门狗（需在 Phase 3 实测确认）**
- Oído 的 `sdkconfig.defaults` 设了 `CONFIG_ESP_TASK_WDT_EN=n`、`CONFIG_ESP_INT_WDT=n` —— 因为长时间计算会触发任务看门狗。**Meshtastic 没有关它。**
- ESP-IDF 默认订阅两个核的 **idle 任务**。ASR 在 core 0 以优先级 1 长时间计算时，core 0 的 idle（优先级 0）可能饿死 → 报 `task watchdog got triggered - idle 0`。
- 缓解顺序（从轻到重）：① 依靠 I2S 阻塞读天然让出（大概率已足够）；② 在 ASR 循环里显式 `esp_task_wdt_reset()`；③ 最后才考虑改 sdkconfig 关 WDT —— **不建议**，那会改变整机行为。

---

## 11. sdkconfig 合并（Oído 的性能前提）

Oído 的速度依赖特定 cache/PSRAM 配置，其 `esp32/firmware/sdkconfig.defaults` 要求：
```
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y      CONFIG_ESPTOOLPY_FLASHMODE_QIO=y
CONFIG_ESPTOOLPY_FLASHFREQ_80M=y       CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y               CONFIG_SPIRAM_SPEED_80M=y
CONFIG_SPIRAM_USE_CAPS_ALLOC=y         CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y
CONFIG_COMPILER_OPTIMIZATION_PERF=y    CONFIG_ESP_MAIN_TASK_STACK_SIZE=32768
CONFIG_ESP32S3_DATA_CACHE_64KB=y       CONFIG_ESP32S3_DATA_CACHE_LINE_64B=y
CONFIG_ESP32S3_INSTRUCTION_CACHE_32KB=y
CONFIG_FREERTOS_HZ=1000
```

**好消息：Meshtastic 已有 per-env 的 sdkconfig 注入机制，L2 的 `-tft` env 正在用它。**
`variants/esp32s3/seeed_wio_tracker_L2/platformio.ini` 的 `-tft` env：
```ini
custom_sdkconfig =
  ${esp32s3_base.custom_sdkconfig}
  ${device-ui_base.custom_sdkconfig}
```
而 `esp32_common.custom_sdkconfig`（根 `platformio.ini:143`）目前只有 MBEDTLS 外置内存 + FATFS exFAT 四项。→ **把 Oído 需要的 cache/PSRAM 项追加到 `-tft` env 的 `custom_sdkconfig` 即可**，机制成熟（该 env 的 `extra_scripts` 注释说明了 pre/post 双趟打补丁的原因）。

**两个必须注意的副作用**：
1. `CONFIG_ESP32S3_DATA_CACHE_64KB=y` 把数据缓存从默认值提到 64 KB，指令缓存 32 KB —— **这两块是从内部 SRAM 里挖的**。Oído 要 96 KB 缓存，会直接压缩 §3 里本已紧张的内部 SRAM 预算。**Phase 0 必须在"加了这些 sdkconfig 之后"再测空闲 heap，否则数字是假的。**
2. 若 Meshtastic/device-ui 已对某一项有不同设定，`custom_sdkconfig` 是后者覆盖还是冲突报错，取决于 platform-espressif32 的处理 —— Phase 0 构建时留意日志。

`CONFIG_ESP_TASK_WDT_EN=n` / `CONFIG_ESP_INT_WDT=n` **不要照搬**，见 §10。
`CONFIG_COMPILER_OPTIMIZATION_PERF=y`（`-O2`）与 `tinyasr` 自己的 `-O2 -ffp-contract=off` 一致，无冲突。

---

## 12. 空间核算结论（**修正 §2 与 §3**，全部基于代码实证）

### 12.1 Flash —— 可解，但 §2 的方案 A 作废

**方案 A（app 槽 3.5 MB）不成立。** 实证：
- 分区表使用分布（`variants/**/platformio.ini` 全量统计）：`default_16MB.csv` **31** 个、`default_8MB.csv` **25** 个、`partition-table-8MB.csv` 5 个、`partition-table-t3s3.csv` 4 个、`no_ota.csv` 1 个。
- `default_8MB.csv` 的 app 槽是 **0x330000 = 3.19 MB**，用它的 25 个板子 CI 全部构建通过 → 非 MUI 构建能塞进 3.19 MB。
- 但把 18 个 `default_8MB.csv` 板子逐个查 `device-ui_base` / `HAS_TFT=1`：**全部 False**（heltec_v3、t-beam-1w、m5stack_cardputer_adv、seeed_xiao_s3、vision_master_e213/e290/t190 …）。**所有 device-ui 板子一律用 6.25 MB 槽。**
- 推论：MUI（LVGL 9.3 + LovyanGFX + PNGdec + libdeflate + 6 套分辨率的 generated 资源 + maps）明显 > 3.19 MB。3.5 MB 槽**太冒险**。

**新的首选方案 D —— 保留双 OTA（4 MB 槽）+ 模型 + LM**，靠压缩 spiffs 腾地方：
```
# Name,   Type, SubType, Offset,   Size,     Flags
nvs,      data, nvs,     0x9000,  0x5000,    # 20 KB
otadata,  data, ota,     0xE000,  0x2000,    # 8 KB
app0,     app,  ota_0,   0x10000, 0x400000,  # 4 MB     -> 0x410000
app1,     app,  ota_1,   0x410000,0x400000,  # 4 MB     -> 0x810000
model,    data, 0x40,    0x810000,0x5C0000,  # 5.75 MB  -> 0xDD0000  (oido_v2b.tnm 5.9 MB)
lm,       data, 0x42,    0xDD0000,0x150000,  # 1.31 MB  -> 0xF20000  (oido_v2.tlm 1.3 MB)
spiffs,   data, spiffs,  0xF20000,0xC0000,   # 768 KB   -> 0xFE0000
coredump, data, coredump,0xFE0000,0x10000,   # 64 KB    -> 0xFF0000
```
偏移逐条验算通过，末尾余 64 KB。**保住了 OTA**，这对产品化很重要。

**spiffs 从 3.375 MB 压到 768 KB 的代价（已核实，可接受）**：
- 该分区由 `FSCommon`（ESP32 上走 **LittleFS**）使用，装的是配置备份/恢复、xmodem 传输，以及历史上的 Web UI 静态资源（`data/static`）。
- **L2 的 platformio.ini 设了 `-D MESHTASTIC_EXCLUDE_WEBSERVER=1`** → Web UI 这个大头消费者在 L2 上本来就不存在。
- 剩余用途（config YAML + 小文件）768 KB 足够；**只有大文件 xmodem 传输会受限**。

**仍然待实测的唯一数字**：L2-tft 的 `.bin` 实际体积是否 ≤ 4 MB。
- 硬上界：CI 会因镜像超出分区而失败，所以必然 ≤ 6.25 MB（当前槽），但这个界不紧。
- 先验估算：基础 esp32s3 构建 ≈2.3–2.8 MB + device-ui/LVGL ≈1.2–1.8 MB → **约 3.5–4.5 MB，处于临界区**。
- 本环境无法下载 Meshtastic release 的 `firmware-*.json` 清单（github.com release CDN 连接超时），所以拿不到确值。**`pio run -e seeed_wio_tracker_L2-tft` 二十分钟即可给出。**
- 判据：≤4 MB → 方案 D（保留 OTA）；4–6 MB → 方案 B（放弃 OTA，单个 6 MB factory app，见 §2）；≤4 MB 但想留 LM 余量 → 方案 C（去掉 `lm` 分区走贪心 CTC）。

### 12.2 RAM —— §3 的判断需要**大幅上修**：内部 SRAM 压力比预想小得多

**核心发现：LVGL 完全不占内部 SRAM。**

`device-ui/include/lv_conf.h`：
```c
#define LV_USE_STDLIB_MALLOC    LV_STDLIB_BUILTIN          // :45
#define LV_MEM_SIZE (RAM_SIZE * 1024U)                     // :62
#define LV_MEM_POOL_INCLUDE     <esp_heap_caps.h>          // :72
#define LV_MEM_POOL_ALLOC(size) heap_caps_aligned_alloc(32, size, MALLOC_CAP_SPIRAM)  // :73
```
L2-tft 的 platformio.ini 设 `-D RAM_SIZE=5120` → **LVGL 整个 5 MB 内存池分配在 octal PSRAM**。

`device-ui/include/graphics/driver/LGFXDriver.h`：L2 未定义 `USE_DOUBLE_BUFFER` / `USE_FULL_DOUBLE_BUFFER`，而 board JSON 的 extra_flags 有 `-D BOARD_HAS_PSRAM` → 走 **:466** 的 `#elif defined(BOARD_HAS_PSRAM)` 分支：
```c
bufsize = LGFX_BUFSIZE;                       // :469  = 153600 (150 KB)
ILOG_DEBUG("LVGL: allocating %u bytes PSRAM for draw buffer ...");  // :473
buf1 = (lv_color_t *)LV_MEM_POOL_ALLOC(bufsize);                    // :474  -> MALLOC_CAP_SPIRAM
```
→ **那 150 KB 绘制缓冲也在 PSRAM**（走 `:446` 的 `MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA` 分支是非 PSRAM 板子）。

**由此得到确定的预算表：**

| 内存 | 总量 | 已确定占用 | 余量 | Oído 需求 | 结论 |
|---|---|---|---|---|---|
| **octal PSRAM** | 8 MB | LVGL 池 **5 MB**（`RAM_SIZE=5120`） | **≈2.7–3 MB** | 流式缓存 **0.3–0.4 MB**（README："0.3–0.4 MB with the streaming caches"）；整段模式 20 s 峰值 2.4 MB | **完全不是问题**，还富余 ~2.3 MB |
| **内部 SRAM** | 320 KB 堆（board JSON `maximum_ram_size: 327680`） | LVGL **0**；mesh 栈 + NodeDB + protobuf 缓冲 + 任务栈（`tft` 16 KB、新 `stt` 8–16 KB）+ I2S DMA(~6–13 KB) + WiFi（若启用） | **未知 —— Phase 0 唯一必须产出的数** | 流式下 **≥72 KB** 空闲 | **临界但大概率够** |

**富余的 ~2.3 MB PSRAM 正好是内部 SRAM 的泄压阀**：`tasr_model_place_weights(m, budget)` 可以把权重矩阵按 `budget` 字节搬进 kind-0（PSRAM）内存，直接换回内部 SRAM。SRAM 缺多少，就从这 2.3 MB 里挪多少 —— 代价是速度（PSRAM 89 MB/s vs 内部 SRAM 3.8 GB/s，见 README "Real time (v2)"）。

**SRAM 若仍不够的降级阶梯（从轻到重）**：
1. `tasr_model_place_weights()` 加大 budget，把更多权重挪去 PSRAM
2. 去掉 `lm` 分区，`tasr_stream_set_decoder(stream, NULL)` 走贪心 CTC（beam search 解码器也吃 SRAM）
3. 缩小 `stt` 任务栈（先用 16 KB 测出实际高水位再压）
4. 减小 `chunk`（`tasr_stream_create(m, 16, 4)` 而非 32），降低单次激活缓冲
5. `TASR_NO_SIMD` 编掉 PIE 内核（省 IRAM，明显变慢，最后手段）

**cache 配置的额外 SRAM 成本，很可能为零**：Oído 要 `CONFIG_ESP32S3_DATA_CACHE_64KB=y` + `INSTRUCTION_CACHE_32KB=y`。arduino-esp32 为 ESP32-S3 + 80 MHz octal PSRAM 的预编译 sdkconfig 极可能已经是这两个值（octal PSRAM 跑 80 MHz 基本要求 64 KB 数据缓存）。**Phase 0 构建日志确认一次即可**；若已是默认值，§11 里"96 KB 从 SRAM 里挖"的担忧不成立。

### 12.3 一句话回答"空间够不够"

- **Flash：够，前提是接受把 spiffs 压到 768 KB（L2 反正排除了 webserver）。** 推荐方案 D 保住双 OTA + 模型 + 语言模型，偏移已验算。唯一悬空的是 `.bin` 是否 ≤ 4 MB，一次 `pio run` 就能定；万一超了退到方案 B（放弃 OTA）。
- **RAM：PSRAM 已确认宽裕（余 ~2.7 MB vs 需 0.4 MB），内部 SRAM 是唯一未知数**，但因为 LVGL 整池在 PSRAM，压力比常规 TFT 板小得多。Oído 要 72 KB，且手里有 `tasr_model_place_weights()` + 2.3 MB 空闲 PSRAM 作为泄压阀，**大概率够，但必须 Phase 0 实测才能下结论** —— 我不会在没有这个数字的情况下说"解决了"。

---

## 13. Phase 0 实测结果（真机 COM83，2026-11-04）

设备已刷 `seeed_wio_tracker_L2-tft`，串口自报身份：
```
INFO | S:B:137,2.8.2.c39728e,seeed_wio_tracker_L2-tft,meshtastic/firmware
INFO | Build timestamp: 1792972800
```
→ **hw_model 137、固件 2.8.2.c39728e、正是本设计针对的 env。**

### 13.1 原始日志（去掉 ANSI 色码）
```
DEBUG |   0 Filesystem files (32768/3538944 Bytes):
DEBUG |   1 Total heap: 273268
DEBUG |   1 Free heap: 216700
DEBUG |   1 Total PSRAM: 8388608
DEBUG |   1 Free PSRAM: 8378956
INFO  |   1 Released BTDM memory because Bluetooth is disabled: heap +2924, free +1916
DEBUG |   1 [DeviceUI] LVGL: allocating 153600 bytes PSRAM for draw buffer (max free PSRAM: 2949108)
DEBUG |   2 Free heap  :  164980 bytes
DEBUG |   2 Free PSRAM : 2577520 bytes
INFO  | 302 [Power] Heap: 162584/276192 bytes free (0 since last), min 126152, largest block 86004, PSRAM: 2577376/8388608 bytes free
INFO  | 302 [Power] MemAudit[periodic]: msgstore=4400 warm=80000 nodedb=25000 satmaps=0 pkthist=12048 tmm=20480 tmm_ni=248000 pktpool(live)=0 total=389928
```

I2C 扫描结果（Phase 1 直接要用）：
```
INFO | ES7243E found at address 0x14     <- 麦克风 ADC
INFO | ES8311  found at address 0x18     <- 喇叭 DAC
INFO | TCA9535 found at address 0x21     <- I/O 扩展（功放/SD/LCD/GNSS 电源）
INFO | AW35615 found at address 0x22     <- USB-C CC
INFO | LP5814  found at address 0x2c     <- 背光
INFO | ADS1115 ADC found at address 0x48 <- 电池
INFO | GT911   found at address 0x5d     <- 触屏
INFO | 7 I2C devices found
```
注意：**Meshtastic 的 I2C 扫描器已经能识别 ES7243E（0x14）**，说明 variant.h 里那些宏不是死代码，总线探测这一层是通的；缺的只是 I2S RX 采集通路（§4）。

### 13.2 RAM —— §12.2 的推算被实测证实

| 项 | §12.2 推算 | **实测** | 判定 |
|---|---|---|---|
| PSRAM 总量 | 8 MB octal | **8,388,608** | ✓ 真机确认 octal 8 MB |
| LVGL 池 + 绘制缓冲落点 | PSRAM | **PSRAM**（日志原文 "allocating 153600 bytes PSRAM for draw buffer"） | ✓ 内部 SRAM 占用为 **0** |
| PSRAM 余量 | ≈2.7–3 MB | 分配绘制缓冲前 `max free PSRAM: 2949108`(2.81 MB)；**稳态 2,577,376 (2.46 MB)** | ✓ 推算命中 |
| 内部堆总量 | 320 KB | **276,192 (269.7 KB)** —— 比 board JSON 的 327680 少，因为 `Released BTDM memory because Bluetooth is disabled` 之后重新核算 | 实测值以此为准 |
| 内部堆空闲 | 未知 | **162,584 (158.8 KB)** | ✓ |
| 历史最低空闲 | 未知 | **min 126,152 (123.2 KB)** | ✓ |
| **最大连续空闲块** | 未知（碎片化风险） | **86,004 (84.0 KB)** | **← 决定性数字** |

**Oído 需求：流式模式下 ≥72 KB (73,728 B) 内部 RAM。**

→ **84.0 KB 可用 vs 72 KB 需求 = 余量 12.0 KB（16.6%）。可行，但薄。**

**为什么这 12 KB 的薄余量是可以接受的**：手里有三个泄压阀，全都指向那 **2.46 MB 空闲 PSRAM**（Oído 流式只需 0.3–0.4 MB，等于有 ~2 MB 纯富余）：
1. `tasr_alloc(size, 0)` → `heap_caps_malloc(MALLOC_CAP_SPIRAM)`，大块分配全部走 PSRAM
2. `tasr_model_place_weights(m, budget)` 把权重矩阵按预算搬进 PSRAM，直接换回内部 SRAM
3. Meshtastic 本身已在 `main.cpp:475` 调 `heap_caps_malloc_extmem_enable(2048)` —— **所有 >2 KB 的 malloc 自动落 PSRAM**，这条与 Oído 的需求天然同向

**MemAudit 分解告诉我们谁在吃内存**（`total=389,928` 含 PSRAM 常驻部分）：
`tmm_ni=248,000`（最大头，TrafficManagement 的 node info 表，**随节点数增长**）、`warm=80,000`、`nodedb=25,000`、`tmm=20,480`、`pkthist=12,048`、`msgstore=4,400`、`satmaps=0`、`pktpool(live)=0`。

**⚠ 三个必须声明的测量条件（会让真实余量比 12 KB 更薄）**：
1. **Region 未设置** → 日志里全是 `lora tx disabled: Region unset` / `lora rx disabled`，**LoRa 实际未工作**。`pktpool(live)=0` 就是证据。设好 region、有真实收发后这一项会增长。
2. **只有 1 个节点**（它自己）→ `nodedb=25,000`、`tmm_ni=248,000` 都是最小值，**随网络规模上升**。
3. **36 秒后进入 powersave**（`enter powersave` / `disable touch, enable button input`）→ LVGL 处于空闲。不过 LVGL 的控件结构与绘制缓冲都在 PSRAM 池里（`LV_MEM_POOL_ALLOC` → `MALLOC_CAP_SPIRAM`），所以对内部 SRAM 影响很小。

→ **Phase 3 必须在"设好 region + 真实 mesh 流量 + 屏幕活跃"的条件下复测一次**，那才是最终判据。当前结论：**大概率够，且有 PSRAM 泄压阀兜底；若复测后最大连续块 < 72 KB，按 §12.2 的五级降级阶梯处理。**

### 13.3 Flash —— spiffs 几乎全空，分区方案可以更激进

```
Filesystem files (32768/3538944 Bytes)
```
→ **LittleFS 只用了 32 KB / 3.375 MB = 0.93%。** §12.1 里"spiffs 可以大幅压缩"的判断被实测证实，而且比预期更极端。

因此把 §12.1 的方案 D 升级为**方案 E（推荐）**：spiffs 压到 320 KB（仍有 10 倍余量），腾出的空间给 app，**双 OTA + 模型 + 语言模型全部保住，16 MB 用满且零浪费**：

```
# Name,   Type, SubType, Offset,   Size,     Flags
nvs,      data, nvs,     0x9000,  0x5000,    # 20 KB     -> 0xE000
otadata,  data, ota,     0xE000,  0x2000,    # 8 KB      -> 0x10000
app0,     app,  ota_0,   0x10000, 0x440000,  # 4.25 MB   -> 0x450000
app1,     app,  ota_1,   0x450000,0x440000,  # 4.25 MB   -> 0x890000
model,    data, 0x40,    0x890000,0x5C0000,  # 5.75 MB   -> 0xE50000   oido_v2b.tnm (5.9 MB)
lm,       data, 0x42,    0xE50000,0x150000,  # 1.31 MB   -> 0xFA0000   oido_v2.tlm  (1.3 MB)
spiffs,   data, spiffs,  0xFA0000,0x50000,   # 320 KB    -> 0xFF0000   (实测仅用 32 KB)
coredump, data, coredump,0xFF0000,0x10000,   # 64 KB     -> 0x1000000
```
偏移逐条验算：`0x10000+0x440000=0x450000` ✓ `+0x440000=0x890000` ✓ `+0x5C0000=0xE50000` ✓ `+0x150000=0xFA0000` ✓ `+0x50000=0xFF0000` ✓ `+0x10000=0x1000000` ✓ —— **正好填满 16 MB，无空洞。**

**唯一剩下的未知数**：L2-tft 的 `.bin` 是否 ≤ 4.25 MB。构建正在跑（`build.log`），跑完即得。判据：
- ≤4.25 MB → **方案 E**（双 OTA + 模型 + LM，最优）
- 4.25–6 MB → 方案 B（放弃 OTA，单个 6 MB factory app）
- 想留更多 app 余量 → 方案 C（去掉 `lm` 分区走贪心 CTC，app 可回到 4.9 MB 双 OTA）

### 13.4 附带关闭的开放项

**§9 开放项 4（IDF 版本）已关闭。** 构建日志：
```
Tool Manager: framework-arduinoespressif32-libs@5.5.5+sha.b774170ff46 has been installed!
Tool Manager: Installing https://github.com/pioarduino/esp-idf/releases/download/v5.5.5/esp-idf-v5.5.5.tar.xz
```
→ Meshtastic 用的是 **ESP-IDF 5.5.5**，Oído 的板级数据基于 **IDF 5.5.1** → **同属 5.5 系，API 与 cache-preload 行为兼容，无需给 `tinyasr` 打补丁。**

**§11 关于 cache 的担忧可以降级**：`sdkconfig` 由 platform-espressif32 55.03.312 + arduino-esp32 3.3.12 决定，实测内部堆总量 276 KB 与 8 MB PSRAM 全量可用（`Total PSRAM: 8388608`，无缩水），说明 octal PSRAM @ 80 MHz 的 cache 配置本来就已是 Oído 期望的形态。**构建完成后核对一次 sdkconfig 即可确认，不再是风险项。**

---

## 14. Phase 0 之后的新发现（**下调 Phase 1 估时**，2026-11-04 复核）

### 14.1 麦克风引脚与音频板级其实已经躺在树里了

`variants/esp32s3/seeed_wio_tracker_L2/variant.h:21-35` 已经声明了完整的 **ADC（麦克风）I2S 引脚组**，与 DAC 组共用时钟：

```c
// ES8311 DAC / AMP
#define HAS_I2S
#define HAS_ES8311
#define DAC_I2S_BCK 11   #define DAC_I2S_WS 12   #define DAC_I2S_DOUT 16
#define DAC_I2S_DIN -1   #define DAC_I2S_MCLK 10
// ES7243E ADC
#define HAS_ES7243E
#define ADC_I2S_BCK 11   #define ADC_I2S_WS 12   #define ADC_I2S_DIN 15
#define ADC_I2S_DOUT -1  #define ADC_I2S_MCLK 10
```

全树搜索结论（`rg -n "ADC_I2S_DIN|ADC_I2S_BCK|HAS_ES7243E" src variants`）：

- 这些 `ADC_I2S_*` 宏 **除定义处外零消费者** —— 麦克风通路是干净的白纸，不会和任何既有代码打架。
- `HAS_ES7243E` 目前只在 `src/detect/ScanI2CTwoWire.cpp:475` 用于 I2C 扫描识别。
- `src/modules/esp32/AudioModule.{h,cpp}` 被 `#if defined(ARCH_ESP32) && defined(USE_SX1280)` 门控（`AudioModule.h:6`）→ **L2 是 SX1262，根本不编译**，无法复用；且它用的是废弃的 `driver/i2s.h` 旧 API（`AudioModule.h:13`），我们应直接上 IDF 5 的 `driver/i2s_std.h`。

`src/platform/extra_variants/seeed_wio_tracker_l2/` **已存在**（`variant.cpp` 5.5 KB、`WakeKey.cpp` 5.2 KB、`WakeKey.h` 1.3 KB），通过 `platformio.ini:118` 的 `build_src_filter = ... -<platform/> +<platform/extra_variants/>` 纳入构建。其中 `variant.cpp` 已经写好：

- `AudioBoard board(AudioDriverES8311, PinsAudioBoardES8311)`
- `PinsAudioBoardES8311.addI2C(PinFunction::CODEC, Wire)` + `.addI2S(CODEC, DAC_I2S_MCLK, DAC_I2S_BCK, DAC_I2S_WS, DAC_I2S_DOUT, DAC_I2S_DIN)`
- `CodecConfig{ADC_INPUT_LINE1, DAC_OUTPUT_ALL, 16bit, RATE_44K}`、`board.setVolume(75)`
- TCA9535 全部电源时序（PA / GNSS / SD / LCD / TP）与侧键唤醒

更关键：**构建依赖 `audio-driver` 库里自带 ES7243E 驱动**

```
.pio/libdeps/seeed_wio_tracker_L2-tft/audio-driver/src/Codecs/es7243/ES7243.h
.pio/libdeps/seeed_wio_tracker_L2-tft/audio-driver/src/Codecs/es7243e/ES7243E.h
```

→ **Phase 1 从「手写 ES7243E 寄存器序列 + 摸电源时序」降级为「在 extra_variants 追加一路 ES7243E 输入板 + 写 16 kHz I2S RX 读取」。** 估时 3–4 天 → **2 天**。参考范例：`src/platform/extra_variants/m5stack_cardputer_adv/variant.cpp`。

### 14.2 模型资产已到位（PC 侧可先验证，不依赖硬件）

| 文件 | 实测字节 | 标称 | 结论 |
|---|---|---|---|
| `models/oido_v2b.tnm` | 5,931,424 | 5.9 MB | ✓ 一致 |
| `models/oido_v2.tlm` | 1,288,852 | 1.3 MB | ✓ 一致 |
| `models/tokenizer.model` | 260,411 | — | ✓ |
| `models/config.json` | 7,030 | — | ✓ |

下载源：`huggingface.co` 被墙，走 `hf-mirror.com`（实测 206 可用）。

**前置半步（不占硬件时间）**：在 PC 上编译 `oido/esp32/host/tasr_cli`，拿一段现成 16 kHz WAV 跑通转写，确认权重文件与 CLI 版本匹配。这一步能把 Phase 2 的最大不确定性提前消掉。

### 14.3 真机与构建状态

- `COM83` 空闲可烧录（`pyserial` 直接打开成功）；在机固件 v2.8.2.c39728e / hw_model 137。
- `pio run -e seeed_wio_tracker_L2-tft` 进行中（13:52 启动，14:19 已过 CMake configure）。产物路径：`.pio/build/seeed_wio_tracker_L2-tft/firmware.bin`。
- **体积决定分区方案**：≤4.25 MB → §13.3 方案 E（双 OTA + model + lm，最优）；4.25–6 MB → 方案 B；想留 app 余量 → 方案 C（去 `lm` 走贪心 CTC）。

---

## 15. 后续工作流程与工期（截至 2026-11-04）

### 15.1 阶段表 —— 关键路径串行，每阶段一个「可失败关卡」

| 阶段 | 目标 | 关卡（Gate，过不了就回退） | 工期 |
|---|---|---|---|
| **P0** ✅ | 真机内存 / Flash / IDF / 引脚实测，方案锁定 | 已完成，见 §13 §14 | 0.5 d |
| **P1** 音频采集 | `extra_variants` 加 ES7243E 输入板；`i2s_std` 16 kHz / 16 bit 单声道 RX；录 WAV 落 SD | PC 上 `tasr_cli` 转写该 WAV，短句词准 > 80% | **2 d** |
| **P2** 引擎上机 | `lib/tinyasr` PlatformIO 移植；`default_16MB_stt.csv` 落盘；模型刷进 `model` / `lm` 分区；`esp_partition_mmap` 加载 | 设备上单句 "send help at grid 45" 转写正确，RTF < 1.0，无 OOM | **4–6 d** |
| **P3** 模块与线程融合 | `SpeechToTextModule`（`OSThread` + `Observable`）；ASR 任务 core0 / prio1 只入队，LVGL 线程出队（照抄 `AsyncMessageLog.h`） | 设好 region + 真实收发跑 30 min：`MemAudit` 不恶化、无 WDT reset、mesh 不被阻塞 | **3–4 d** |
| **P4** UI 融合 | `INPUTDRIVER_VOICE_TYPE` 驱动 + `IInputSource` 策略 + 麦克风按钮（点击注入不发送 / 长按注入并发送） | 按住说话 → 文本进输入框，交互与 QuickChat 一致 | **3–4 d** |
| **P5** 配置下发（可选 / 可并行） | `protobufs` 加 `AudioConfig` 字段 8–12；Admin 远程开关 STT | 对端节点能远程启停 STT | **2 d** |
| **P6** 打磨 + 交付 | 噪声 / 距离实测调参、文档、demo 视频脚本、上游 PR 描述 | 上游 CI 通过 + maintainer 愿意收 | **3–5 d** |

**关键路径合计 ≈ 17.5–23.5 工作日**（P5 并行则 15.5–21.5）。按每周 5 个有效工作日：

- **M1 可演示 demo（内部 / 视频）：约 3.5–4.5 周** ← marketing 诉求的最早兑现点
- **M2 可提上游 PR：M1 + 1 周 ≈ 5–6 周**
- **M3 可出厂 / 随固件发布：M2 + 2–4 周**（主要由法务与上游 review 决定，不在工程控制内）

> 若只要「能拍视频的最小 demo」：砍掉 P5、把 P4 简化为「只注入不发送」→ **可压缩到 2.5–3 周**。

### 15.2 分工

**我可以直接做（有 COM83 + 完整源码树）**

- `lib/tinyasr` PlatformIO 移植：保留 `-O2 -ffp-contract=off`，`tasr_alloc` 钩子接 PSRAM，`esp_partition_mmap` 加载权重
- `default_16MB_stt.csv` 分区表落盘 + `board_build.partitions` 接线（**只写进 `-tft` env，不动其它板子**）
- `extra_variants/seeed_wio_tracker_l2` 追加 ES7243E 输入板 + `i2s_std` RX 采集
- `SpeechToTextModule` 骨架 + `src/modules/Modules.cpp` 并列注册 + `-D MESHTASTIC_INCLUDE_STT` / `-D INPUTDRIVER_VOICE_TYPE=1`
- `device-ui`：`VoiceInputDriver` + `IInputSource` 策略 + 麦克风按钮
- 构建 / 烧录 / 串口日志验证 / 内存复测 / 文档与 PR 描述

**需要你做（我做不了）**

1. **法务**：`oido_v2b.tnm` / `oido_v2.tlm` 权重是 **CC-BY-SA-4.0**，能否随 Seeed 固件与营销物料分发 —— **最大不确定性，建议本周就发起**
2. **内部确认**：L2 Pro 出厂固件用的是上游 `seeed_wio_tracker_L2` variant，还是内部 fork？若是 fork，改动该落到哪个仓库 / 分支
3. **物理测试**：户外两台 L2 实测（距离、按住说话手感、真实噪声下的词准率）
4. **营销口径**：demo 视频拍什么场景（建议：无网徒步，按住说 "send help"，对端手机 App 收到文本）
5. **VSCode + Claude 代跑的活**：我给现成提示词（PC 端 `tasr_cli` 验证 / 独立录音工程）

### 15.3 风险与缓冲（已计入上面的区间）

| 风险 | 影响 | 缓解手段（已备） |
|---|---|---|
| 内部 SRAM 余量只有 **12 KB**（最大连续块 84.0 KB vs Oído 需 72 KB） | +3–5 d | 2.46 MB 空闲 PSRAM、`tasr_model_place_weights()`、Meshtastic 已有 `heap_caps_malloc_extmem_enable(2048)`（`main.cpp:475`） |
| **I2S 时钟域**：ES8311 DAC 44.1 kHz 与 ES7243E ADC 16 kHz 共用 BCK=11 / WS=12 / MCLK=10 | +3–5 d | 现有 AudioBoard 已按 full-duplex 声明；统一到单一采样率，或用第二个 I2S 外设 |
| 看门狗 / LVGL 线程被 ASR 阻塞 | +1–2 d | ASR 只入队、core 0 prio 1（与 `tft` 任务同级，`tftSetup.cpp:432`）；绝不在 ASR 线程碰 LVGL |
| 法务否决 CC-BY-SA | **+1–2 周** | 换权重 / 重训 / 只在 demo 用、不随固件分发 |
| 上游 review 周期 | 不可控 | M2 之后不计入工期 |

### 15.4 今天就能推进的三件事

1. 等构建结束 → 读 `firmware.bin` 体积 → 敲定分区方案（E / B / C）→ 落盘 `default_16MB_stt.csv`
2. 设备上设 region（如 `US`）+ 关 powersave → 抓 5 min 真实条件日志复测内存判据（§13 的测量条件不成立：region 未设、`pktpool(live)=0`、36 s 后进省电）
3. PC 上编译 `tasr_cli` 跑通一个现成 WAV → 提前消掉 P2 的权重兼容性风险（不占硬件时间）

---

## 16. 实施状态（2026-11-04，单机 demo 快速路径）

### 16.1 范围收窄

不提上游 PR、不做多机、不做 30 分钟压测、不碰 `protobufs`。目标只有一句：**一台 L2 Pro 上「按住说话 → 出英文文本」**。P5/P6 的 PR 部分整段删除，P3 的压测关卡降级为「串口看内存与 RTF」。

### 16.2 必须纠正的关键发现：模型是 NeMo，不是流式 tinyasr

`models/oido_v2b.tnm` 前 4 字节是 `TNM1`（`oido_v2.tlm` 是 `TLM1`）。按 `oido/esp32/host/tasr_cli.c:80` 的判据：

```c
const int nemo = msz >= 4 && !memcmp(mb, "TNM1", 4);
```

→ 走的是 **`tasr_nemo_*`（NVIDIA NeMo Conformer-CTC small，13 M 参数 int8）**，不是 §7 骨架里写的 `tasr_model_load` / `tasr_stream_feed`。真实 API（`oido/esp32/components/tinyasr/include/tasr_nemo.h`）：

- `tasr_nemo_load(blob,size)` / `_free` / `_blob_bytes` / `_weight_bytes` / `_place_weights(m,budget)`
- 整句：`tasr_nemo_transcribe(m, pcm, n, dec, text, maxlen, logit_sink, max_frames, n_frames)`，`dec=NULL` 即 greedy CTC
- 流式：`tasr_nemo_stream_new(m, chunk, left, dec)`（chunk/left 传 0 = 模型自带默认 16/128）+ `_feed` / `_text` / `_finish`；先 `tasr_nemo_stream_supported(m)` 判断
- 词表 1024 token（beam decoder 的 `V1 = 1025`），不是 257

→ **§7.2 骨架按此改写。LM（`oido_v2.tlm`）第一版不接，走 greedy CTC，因此分区表不需要 `lm` 分区。**

顺带：`tasr_seg.c` 是现成的 **AGC + 能量 VAD 分段器**（320 样本一块，`TASR_SEG_PREROLL 3200`，静音 0.8 s 收尾）。以后做「免按键、自动断句」直接用它，不用自己写。

### 16.3 §4 的 I2S 时钟域风险已被寄存器序列解掉

`audio-driver/src/Codecs/es7243e/ES7243E.h` 的 init 序列写死了：

- `reg 0x06 = 0x03` → SCLK = MCLK/4
- `reg 0x07 / 0x08 = 0x00 / 0xFF` → LRCK = MCLK/256
- `reg 0x20 / 0x21 = 0x1A` → PGA +30 dB
- 末尾 `reg 0x00 = 0x80` → **从模式**（ESP32 做 I2S 主机）

反推出唯一自洽配置：16 kHz 采样 → **MCLK 4.096 MHz、BCK 1.024 MHz（= 64 × fs，即 32-bit slot）**。对应 `i2s_std`：`mclk_multiple = I2S_MCLK_MULTIPLE_256`、`data_bit_width = 16BIT`、`slot_bit_width = 32BIT`，立体声读回取左声道。

ES7243E 的 7-bit I2C 地址是 **0x14**（`src/configuration.h:370` `ES7243E_ADDR`；真机启动日志已确认 `ES7243E found at address 0x14`），挂在 `Wire`（SDA 47 / SCL 48）上，与 ES8311(0x18)、TCA9535(0x21) 同总线。

**端口不冲突**：蜂鸣器 / RTTTL 走 `AudioThread`，用的是 `AudioOutputI2S(1, EXTERNAL_I2S)`（`src/AudioThread.h:110`）→ **I2S_NUM_1**；麦克风放 **I2S_NUM_0**。`AudioModule` 因 `USE_SX1280` 门控根本不编译（`src/modules/esp32/AudioModule.h:6`）。

### 16.4 SD 卡当前不可用 → 测试资产走 LittleFS

真机日志：

```
E (6942) sdmmc_common: sdmmc_init_ocr: send_op_cond (1) returned 0x107
E (6942) vfs_fat_sdmmc: sdmmc_card_init failed (0x107).
[SD_MMC.cpp:312] begin(): Failed to initialize the card (0x107). Make sure SD card lines have pull-up resistors in place.
```

没插卡或卡拉不住。仓库 `data/` 目录本来就是空的、`spiffs` 分区几乎全空 → **正好当资产分区用**，测试音频随 `uploadfs` 一起刷进去，不依赖 SD。

### 16.5 已落地的代码（均在 `research/meshtastic-firmware`）

| 文件 | 作用 |
|---|---|
| `lib/tinyasr/`（11 个源文件 + `library.json`） | Oído 引擎移植为 PlatformIO 库；`-O2 -ffp-contract=off -DESP_PLATFORM -DCONFIG_IDF_TARGET_ESP32S3=1`。最后这个 define 是**开启 PIE SIMD 与 D-cache 预取的开关** —— `kernels.c:14` 在包含任何 sdkconfig 之前就要判定，不加就会静默退化成无 SIMD（RTF 直接爆掉） |
| `default_16MB_stt.csv` | **方案 G**：双 OTA app（各 4.625 MiB）+ `model` @0x950000/0x5C0000 + spiffs 0xE0000 + coredump。去掉 `lm` 分区 |
| `variants/esp32s3/seeed_wio_tracker_L2/platformio.ini:61` | 只在 `-tft` env 加 `board_build.partitions = default_16MB_stt.csv` |
| `src/platform/esp32/L2AudioInput.{h,cpp}` | ES7243E 直接用 `Wire` 写寄存器 + `i2s_std` RX 16 kHz；`read()` 立体声转单声道并统计峰值，一眼分辨「麦克风静音」还是「接线/位宽错」 |
| `src/modules/esp32/SpeechToTextModule.{h,cpp}` | `esp_partition_mmap` 映射 model 分区 → `tasr_nemo_load` → 装 `tasr_alloc` 钩子（kind 1 → 内部 SRAM，kind 0 → PSRAM）→ 自检 → 按住 GPIO0 说话 |
| `src/modules/Modules.cpp` | 并列注册，与 `AudioModule` 同一个 `#ifdef ARCH_ESP32` 块 |
| `data/stt/test_en.wav` | Windows SAPI（Microsoft Zira, en-US）合成的 16 kHz 单声道测试句，2.56 s / 40880 样本。**ground truth = "Send help at grid four five."** |
| `L2_Demo/flash_stt.ps1` | 一条命令烧 app + LittleFS + model 分区 |

门控改用 Meshtastic 自己的惯例，**没有动 `build_flags`**：

```cpp
#if defined(ARCH_ESP32) && defined(SEEED_WIO_TRACKER_L2) && defined(HAS_TFT) && !defined(MESHTASTIC_EXCLUDE_STT)
```

原计划的 `-D MESHTASTIC_INCLUDE_STT` 会改动全局 flag → 每次改都要全量重编所有库。换成 `MESHTASTIC_EXCLUDE_STT` 后既能一键关掉，又不碰全局 flag。（注意：新增 `lib/tinyasr` 本身仍会让 LDF 改变全局 include 路径，触发一次全量重编，约 1 小时，是一次性成本。）

### 16.6 开机后串口应该看到什么

```
STT: armed, bring-up in 20000 ms
STT: 'model' partition at 0x950000, 6029312 bytes
STT mem before load: internal free ... largest ... , PSRAM free ...
STT: engine ready, blob 5931424 bytes, weights ... bytes, streaming ...
STT: decoding bundled clip /stt/test_en.wav (40880 samples, 2.56 s)
STT: N encoder frames, 2.56 s audio decoded in X.XX s cpu (RTF 0.xx)
STT clip transcript: "send help at grid four five"
STT mic: ES7243E configured at 0x14
STT mic: I2S0 RX up, 16000 Hz, MCLK 4096000 Hz, BCK 1024000 Hz, 32 bit slots
STT: mic probe read 4000 samples, peak NNNN
STT: ready - hold the user button (GPIO 0) and speak English
```

之后按住机身用户键说英文，松开打印 `STT heard: "..."`。

### 16.7 故障判据（看日志就能定位）

| 现象 | 原因 / 处置 |
|---|---|
| `STT: no 'model' partition` | 分区表没刷进去，重跑 `flash_stt.ps1` |
| `tasr_nemo_load failed` | model 分区内容不对，确认 `oido_v2b.tnm` 烧到了 0x950000 |
| `after load` 的 `internal free` < 20 KB | 把更多 kind-1 降级到 PSRAM，或调小 `STT_MAX_UTTERANCE_SEC` |
| `mic probe ... peak` 长期 < 64 | ES7243E 增益 / slot 位宽不对，先把 `slot_bit_width` 对照试成 16BIT |
| RTF > 1 | PIE SIMD 没生效（查 `-DCONFIG_IDF_TARGET_ESP32S3=1`）或 cache 被 PSRAM 抢占 |
| 转写为空但 peak 正常 | 先跑 `transcribeWavFile` 的内置 clip 对照，区分「引擎问题」还是「麦克风问题」 |

### 16.8 收窄后的工期

| 里程碑 | 内容 | 剩余 |
|---|---|---|
| **A** | 引擎在设备上跑通（本轮构建 + 烧录 + 内置 clip 出正确文本） | 1–2 天 |
| **B** | 麦克风采集 + 按住说话出文本 | 1–2 天 |
| **C** | 文本进消息输入框 / 直接发 `TEXT_MESSAGE_APP` | 1–2 天 |
| **D** | 触屏麦克风按钮 + 录音状态提示（LVGL） | 2–3 天 |
| **E** | 噪声与增益调参 + demo 视频 | 1–2 天 |

**到「单机能演示」≈ 6–11 个工作日（1.5–2 周）**，比原估的 3.5–4.5 周短一大截：砍掉了多机、压测、protobufs 与上游 PR。其中 A、B 已经写完代码，只等本轮构建与烧录验证。

## 17. 触屏麦克风按钮（lv_layer_top 叠加层）

### 17.1 为什么是叠加层而不是改 MUI

MUI（`meshtastic-device-ui`）是 lib_deps 里的外部库，直接改它会在重新拉库时被覆盖。而 MUI 的界面就是 LVGL 9.3，
屏幕上一共有 `screen`（MUI 页面）、`lv_layer_top()`、`lv_layer_sys()` 三层。我们只在 **top layer** 上创建一个按钮：

- 不动库，不改任何 MUI 页面；
- 层级在所有页面之上，切页/弹窗都不会被盖住；
- 触摸命中测试从顶层往下找，所以按钮优先级最高。

### 17.2 线程模型

LVGL 不是线程安全的，MUI 所有 `lv_*` 调用都在 `tft` 任务里（`tft_task_handler()` → `deviceScreen->task_handler()`）。
所以按钮的创建和状态刷新都挂在同一个循环里：

```cpp
deviceScreen->task_handler();
sttTouchUiTick();      // 只在这一行做 LVGL 操作
deviceScreen->sleep();
```

`sttTouchUiTick()` 第一帧创建按钮，之后每帧只比对状态（是否在录音、第几秒），状态没变就什么都不做。

### 17.3 录音不再阻塞主循环

原实现是在 `runOnce()` 里同步读 I2S，按住多久就阻塞多久（最长 8 s），期间 LoRa 收发、按键、UI 全部停摆。
现在拆成两个任务：

| 任务 | 栈 | 职责 |
|---|---|---|
| `stt_capture` | 4 KB，core 0 | 等信号量 → 读 I2S 直到松手或满 8 s → 交给解码 |
| `stt_decode` | 16 KB，core 0 | `tasr_nemo_transcribe()`，约 2.5 s |

`startCapture()` / `stopCapture()` 只是翻标志位 + 给信号量，UI 线程调用它们代价是微秒级。

### 17.4 操作方式

1. 任意页面右下角有圆形 `MIC` 按钮；
2. **按住**它 → 变红并显示 `REC 1.2s` 计时；
3. **松开** → 串口打印 `STT: captured ...` / `STT heard: "..."` / `STT: sent transcript ...`；
4. 本机聊天列表里出现自己发的消息，同频道其它节点也会收到。

物理按键仍然保留：GPIO0（USER 键）按住说话同样有效。注意 **WAKE-UP 键不参与录音**，它由 `WakeKey.cpp`
接管，只负责亮/灭屏。
