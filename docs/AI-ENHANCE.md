# AI 增强：分项评分与自动标签

这一层是对原版「拉普拉斯方差 + 组内相对排序」的补充，目标是让“是否合焦”
变成可解释的分项分，并且在桌面版里顺手把标签和星级写进 XMP。

所有计算都在本机：网页只把**文件路径**发给 `127.0.0.1` 上的本地服务，
图片本身不出机器，也不调用任何在线 API。

## 为什么不再只看拉普拉斯方差

原版用固定窗口的拉普拉斯方差，遇到两个典型误判：

1. 高反差的虚焦背景（树枝、水花）方差比低反差但实焦的主体还高；
2. 机内锐化烘焙进预览之后，绝对方差随机身、锐化档位、ISO 大幅漂移；
3. 完全看不到运动模糊——方差对「拖影」和「弥散」给出的数值差不多。

因此新实现拆成五件事分别测量，再加权汇总。

## 指标

全部基于长边 1600px 的预览（RAW 取内嵌 JPEG，其余格式直接解码），
在亮度平面上计算，分块大小 32px。

### 对焦 `focus`

- 块内梯度幅值均值 ÷ 块内亮度标准差 = **反差归一化斜率**，抵消“越亮越花”的偏差；
- 只在标准差 ≥ 6（有纹理）的块上统计；`focus_slope = 0.6 × P75 + 0.4 × P90`。
  P75 代表整体细节，P90 代表最锐的那部分边缘（眼睛、喙、叶脉）；只用 P75 会把
  羽毛平滑的实焦主体误判成偏软，只用 P90 又容易被背景高反差细节带偏；
- 0–100 用对数比值映射：`log(slope / 0.10) / log(0.60 / 0.10)`。
  低端（0.10–0.20）拉开差距，高端不会早早饱和。

降采样尺度只作为诊断字段 `slope_half` 输出，不参与打分：下采样会把
“幅度衰减”和“细节丢失”混在一起，反而掩盖虚焦。

### 运动模糊 `motion`

- 先做 5×5 均值高通，只保留中高频细节；
- 在 0° / 45° / 90° / 135° 四个方向计算梯度能量，取 `最小 / 最大` 作为方向能量比；
- 比例低于 0.18 视为明显拖影，高于 0.55 视为正常；
- 再用整体清晰度打折：`risk = dir_risk × (1 - 0.7 × focus_norm)`，避免把
  “主体只有横向线条”的实焦画面误判为手抖。

局限：只对接近轴向的拖影敏感，45° 拖影会分散到两个方向而被低估；
画面本身极端单一方向时仍可能误报。

### 曝光 `exposure`

高光裁切（≥250）比例、暗部死黑（≤4）比例、中调占比与直方图熵。
高光裁切扣分最重；夜景大面积暗部只有在整体偏暗（平均 < 55）时才算欠曝。

### 噪声 `noise`

对 3×3 均值高通残差，在低纹理块上取中位数 × 1.4826 作为 σ 的鲁棒估计；
ISO ≥ 3200 时额外扣分（ISO 来自 ExifTool，可选）。

### 主体 `subject`

有 AF 信息（目前仅 Nikon NEF 的 AFInfo2）时用对焦框，否则用中心 62% 区域；
统计该区域内纹理块占比，占比越高说明主体细节越实在。

## 综合分与星级

```
composite = Σ(part_i × weight_i)     # 默认 45/15/15/10/15，见 config/scoring.json
```

判定与星级刻意分开，避免“曝光好、主体大”把虚焦帧抬到高星：

| 判定 | 条件 | 星级 |
| --- | --- | --- |
| `blur` | focus < 30 | 综合 ≥ 55 给 1 星，否则 0 星 |
| `border` | focus < 50，或纹理占比 < 0.15（无法判断） | 1–3 星 |
| `sharp` | focus ≥ 50 | 2–5 星（72/82/90 分档） |

综合分是 0–100 的加权和，UI 里会逐项显示，改权重即可实时重排。
`AI/` 前缀标签只增删自己写的条目，`xmp:Rating` 只写星级，其余 XMP 节点原样保留。

## 标定与已知边界

- **绝对分数是近似值**：预览是相机直出 JPEG，机内锐化已经烘焙，无法还原传感器
  真实对焦精度。分数更适合同一机身、同一组连拍的相对比较。
- 当前常量（`SLOPE_LO=0.10`、`SLOPE_HI=0.60`、判定阈值 50/30）在 Nikon Z 30 +
  50–250mm 的鸟类样片上验证过：

  | 样本 | 原图 focus | 2px 模糊 | 判定 |
  | --- | --- | --- | --- |
  | 近距离翠鸟（细节多） | 100 | 54 | 原图 sharp，2px 起 border |
  | 冠鸠（羽毛平滑） | 67 | 32 | 原图 sharp，2px border |
  | 中等距离鸣禽 ×2 | 85–90 | 45 | 原图 sharp，2px border |

- 换机身、换锐化档位、常拍低细节题材（雾、剪影、纯色天空）时应重新标定：

  ```bash
  python tools/calibrate.py "D:\Photos\2026-07-04" --limit 40 --blur
  ```

  工具会打印每张的 P75/P90/混合斜率、focus 分与判定，并用 2px 模糊做负样本，
  给出 `SLOPE_LO` / `SLOPE_HI` 的建议值；分离度低于 2× 时不要硬调。
- 低纹理画面（`texture_ratio < 0.15`）只标 `border`，不判虚焦，宁可让人眼复核。
- 组内相对排序仍然保留：`applySuggested` 会给每组综合分最高的一张补一星，
  且不覆盖用户手动改过的星级（`starTouched`）。
- 自动标签是零样本 CLIP（ViT-B/32），只能给出大类（鸟类 / 军舰 / 船只 / 飞机 /
  野生动物…），给不出鸟种或舰级。实测：
  - 鸟类照片 `bird` 0.98–0.99；
  - 南宁舰（052D）`warship` 0.99–1.00，同批军港照片全部命中；
  - 货轮 → `ship`，帆船 → `boat`，客机 → `aircraft`，跑车 → `vehicle`；
  - 3D 渲染壁纸会落入拒识类 `other`，不输出标签。
- 标签用「每类提示词向量取平均后再 softmax」，避免单条措辞过度自信；
  `other` 只参与拒识、不写成 `AI/other`。改标签或提示词后递增
  `LABELS_VERSION`，旧缓存会自动失效重算。
- 兼容性：transformers 5 的 `get_text_features` / `get_image_features` 返回
  **未投影**的模型输出，直接点积会让图文空间错位（表现为“航母 → 家庭”）。
  这里显式走 `text_model → text_projection` 与 `vision_model → visual_projection`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/ai/status?token=` | 服务版本、ExifTool / CLIP / 缓存状态 |
| POST | `/api/ai/analyze?token=` | `{items:[{id,path}], tags, force}`，逐项返回指标、评分、标签 |
| POST | `/api/xmp/write?token=` | `{items:[{path,rating,tags,label}]}`，合并写 XMP |

桌面版另有 pywebview 的 `write_xmp(items)`，与上面的 HTTP 接口共用
`service/xmp.py`，支持同时写星级与 `AI/` 标签。

## 配置

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `PHOTOPLUS_SCORING` | `config/scoring.json` | 自定义权重文件 |
| `PHOTOPLUS_LIBRARY` | `~/Library/Application Support/PhotoCull/library.sqlite3` | 分析缓存 |
| `PHOTOPLUS_CLIP_MODEL` | `openai/clip-vit-base-patch32` | 换 CLIP 权重 |
| `PHOTOPLUS_CLIP_ALLOW_DOWNLOAD` | 未设置 | 设为 `1` 才允许联网下载权重 |

ExifTool 为可选项（用于 ISO、机身、对焦框）。程序会按以下顺序查找：
`PATH` → 环境变量 `PHOTOPLUS_EXIFTOOL` → 常见安装位置
（`%LOCALAPPDATA%\Programs\ExifTool\ExifTool.exe`、`C:\Program Files\ExifTool`、
`/usr/local/bin/exiftool`、`/opt/homebrew/bin/exiftool`）。
检测不到就少这些字段，不影响评分。Nikon Z 30 实测能读到 ISO / 机身 / 镜头，
但机内不写 `FocusLocation`，因此仍退回中心区。

## GPU 加速

自动标签默认走 GPU：装了 CUDA 版 torch 时 `clip.device` 为 `cuda`，否则 `cpu`。
换装命令：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu126
```

加载失败（显存不足、fp16 不支持、驱动过旧）会清空模型并退回 CPU 重试一次，
不会让“自动标签”整块不可用。判断是否真的在用 GPU，看
`/api/ai/status` 里的 `clip.device`，不要依赖 `nvidia-smi`。

## 性能

实测环境：Nikon Z 30 NEF（30MB/张，内嵌 5568×3712 JPEG），RTX 3050 Laptop 4GB。

| 阶段 | 耗时 |
| --- | --- |
| 取内嵌预览 + 缩到 1600px | ~0.35 s |
| 指标计算 | ~0.26 s |
| CLIP 推理（CUDA fp16） | ~0.03 s |
| 整条流水线（含 ExifTool、缓存写入） | **~0.87 s/张**（8 张热跑 6.9 s） |

- 命中 SQLite 缓存后约 2 ms/张，缓存键为路径 + 大小 + mtime；
- CLIP 显存峰值约 320 MB，4GB 显卡余量充足；
- GPU 不可用（无卡、驱动异常、fp16 不支持）会自动退回 CPU，功能不变；
- 注意：`nvidia-smi` 报 NVML 初始化失败并不代表 CUDA 不可用——
  本机就是 `nvidia-smi` 失败但 `torch.cuda.is_available()` 为 True，
  判断标准应是 torch 的返回值，而不是 NVML 工具。

## 测试

```bash
python -m unittest discover -s tests -v
node tests/focus.test.cjs
```

Python 侧 18 项（指标、打分、XMP 合并、EXIF 辅助），不需要 torch；
node 侧是原版 focus-core 回归。前端可用 `python app.py --smoke` 做无窗口冒烟：
会检查页面、AI 面板、`window.AIEnhance`，并从界面内请求一次 `/api/ai/status`。
