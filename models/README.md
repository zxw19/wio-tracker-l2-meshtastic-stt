# 模型文件

| 文件 | 大小 | 说明 |
|---|---|---|
| `oido_v2b.tnm` | 5.93 MB | 主模型：NeMo Conformer-CTC small，int8，英文，流式训练 |
| `oido_v2.tlm` | 1.29 MB | 可选语言模型（本固件未启用） |
| `tokenizer.model` | 254 KB | SentencePiece 词表（训练侧使用） |
| `config.json` | 7 KB | 训练配置 |

权重来自 **Oído**（<https://github.com/lokutor-ai/oido>），遵循 GPL-3.0。
`oido_v2b.tnm` 是自定义容器格式（magic `TNM1`），由 `tasr_nemo_load()` 解析，
**必须与 `lib/tinyasr` 的引擎版本配套**，不能与上游其它版本混用。

烧录位置：raw 分区 `model`，偏移 `0x950000`：

```powershell
python $env:USERPROFILE\.platformio\packages\tool-esptoolpy\esptool.py `
  --chip esp32s3 --port COM83 --baud 921600 write_flash 0x950000 models\oido_v2b.tnm
```
