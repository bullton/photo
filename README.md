# 焦点挑选 · Focus Picker

本地照片合焦筛选工具，面向野生动物 / 鸟类等需要连拍精选的场景。照片只在本地处理，不上传云端。

- 网页版：直接打开 `index.html`（Chrome / Safari / Edge）。
- 桌面版：`焦点挑选.app`（macOS，内置本地服务，无浏览器地址栏，支持原生文件选择与写 XMP）。

## 功能

- 读取 NEF / NRW / CR2 / CR3 / ARW / DNG / RAF / ORF / RW2 / PEF 等 RAW 的内嵌预览，无需外部解码器；也支持 JPG / PNG / TIFF 等常见格式。
- 按拉普拉斯方差评估清晰度：有 AFInfo2 对焦信息时按对焦框 / 主体区评分，没有时退回画面中心区评分。
- 按拍摄时间 + 感知哈希 + 亮度 + 颜色布局自动分组，组内按清晰度排序；分组灵敏度可选 更宽松 / 宽松 / 标准 / 严格。
- 1–5 星评分，写入同名 XMP 侧车文件的 `xmp:Rating`（保留 Lightroom 修图参数），也可导出 CSV 清单。
- 大图查看支持适合窗口 / 50% / 100% / 25–200% 缩放条，按对焦框或画面中心定位；焦点分布热力图默认关闭。
- 按组平均分与单张分数统一从高到低排序，方便逐张过片。
- 导入时可选择「仅 RAW」，跳过 JPG / PNG 等；分析进度显示剩余时间估算。
- AI 增强（桌面版）：反差归一化对焦分、运动模糊风险、曝光 / 噪声 / 主体分项，综合分加权汇总；可一键应用建议星级，组内最佳标 `AI/Keep`、重复帧标 `AI/Duplicate`，并把 `AI/` 标签写入 XMP；星空 / 夜景不会被建议成废片。详见 [docs/AI-ENHANCE.md](docs/AI-ENHANCE.md)。

## 使用

网页版：

```bash
open index.html
```

桌面版：

```bash
open "焦点挑选.app"
```

从源码运行桌面版：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 可选：启用自动标签（CLIP，需要 torch + transformers）
.venv/bin/pip install -r requirements-ai.txt
# 有 NVIDIA 显卡时改装 CUDA 版，标签推理会走 GPU
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu126
.venv/bin/python app.py
```

打包 macOS App（需要 PyInstaller）：

```bash
pyinstaller --noconfirm --windowed --name "焦点挑选" \
  --icon assets/app.icns \
  --add-data "index.html:." --add-data "focus-core.js:." app.py
```

## 测试

```bash
node tests/focus.test.cjs

# AI 评分 / XMP 合并（纯 Python，不需要 node 或 torch）
python -m unittest discover -s tests -v
```

## 结构

- `index.html` — 界面、缓存、评分流程、桌面桥接
- `focus-core.js` — 清晰度评分与分组算法
- `ai-core.js` — AI 增强前端接线（调用本地服务、渲染分项、应用建议星级）
- `service/` — 本地分析服务：预览提取、指标、打分、CLIP 标签、XMP、缓存
- `config/scoring.json` — 综合分权重
- `tools/calibrate.py` — 用你自己的照片标定对焦阈值（可选）
- `docs/AI-ENHANCE.md` — AI 增强的指标定义、标定方法与已知边界
- `app.py` — pywebview 桌面壳（本地 HTTP 服务、文件选择、XMP / CSV 写入）
- `assets/` — 应用图标源文件（SVG / PNG / ICNS）
- `tests/` — node 与 Python 单元测试

## 说明

- HEIC 需先转成 JPEG。
- 星级写入 XMP 仅对 RAW 生效：已有 XMP 只更新 Rating，不覆盖其他字段；从未评分的照片不会生成 XMP。
- 清晰度分已改为绝对刻度（不再随导入批次漂移），但不同机身/镜头的绝对水平仍有差异。
- AI 分数基于相机内嵌预览计算，绝对分数是近似值；换机身或机内锐化档位后应重新标定阈值（见 `docs/AI-ENHANCE.md`）。
- 装 ExifTool（可选）可补 ISO / 机身 / 对焦框：程序会自动查找 `PATH` 与常见安装目录，也可用 `PHOTOPLUS_EXIFTOOL` 指定路径。
