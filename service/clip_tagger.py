"""CLIP 零样本自动打标（可选能力）。

torch / transformers / 权重任一缺失时自动降级为“不可用”，不会阻塞评分。
默认只读本地缓存，离线可用；需要首次下载权重时设置
PHOTOPLUS_CLIP_ALLOW_DOWNLOAD=1。
"""

from __future__ import annotations

import os
import threading

DEFAULT_MODEL = "openai/clip-vit-base-patch32"

# 改标签或提示词后递增，用来让旧的分析缓存失效。
LABELS_VERSION = 2

LABELS: dict[str, dict] = {
    "bird": {"zh": "鸟类", "prompts": ["a photo of a bird", "a close-up photo of a bird in the wild", "a bird in flight"]},
    "wildlife": {"zh": "野生动物", "prompts": ["a photo of a wild animal", "a wildlife photo of an animal in nature"]},
    "pet": {"zh": "宠物", "prompts": ["a photo of a pet cat or dog", "a portrait of a pet"]},
    "warship": {"zh": "军舰", "prompts": ["a photo of a navy warship at sea", "a photo of a military ship with guns and radar", "a photo of a destroyer or frigate"]},
    "ship": {"zh": "船只", "prompts": ["a photo of a large ship at sea", "a photo of a cargo ship or cruise liner", "a photo of a ferry on the water"]},
    "boat": {"zh": "小船", "prompts": ["a photo of a small boat", "a photo of a sailboat on the water", "a photo of a fishing boat"]},
    "aircraft": {"zh": "飞机", "prompts": ["a photo of an airplane", "a photo of an airliner on the runway", "a photo of a fighter jet in the sky"]},
    "vehicle": {"zh": "车辆", "prompts": ["a photo of a car", "a photo of a sports car", "a photo of a motorcycle", "a photo of a truck or bus on the road"]},
    "landscape": {"zh": "风光", "prompts": ["a landscape photo", "a wide scenic view of nature"]},
    "astrophotography": {"zh": "星空", "prompts": ["an astrophotography photo of the milky way and stars", "a night sky full of stars"]},
    "night_scene": {"zh": "夜景", "prompts": ["a night photo of a city or building", "a low light scene at night"]},
    "portrait": {"zh": "人像", "prompts": ["a portrait photo of a person", "a close-up portrait of a face"]},
    "family": {"zh": "家庭", "prompts": ["a family photo of people together", "a candid photo of people at home"]},
    "street": {"zh": "街拍", "prompts": ["a street photography photo of people in a city", "a candid street scene"]},
    "macro": {"zh": "微距", "prompts": ["an extreme close-up macro photo of a small subject", "a macro photo of an insect"]},
    "flower": {"zh": "花卉", "prompts": ["a close-up photo of a flower", "a photo of blooming flowers"]},
    "architecture": {"zh": "建筑", "prompts": ["an architecture photo of a building", "a photo of an interior or building"]},
    "food": {"zh": "食物", "prompts": ["a photo of food on a table", "a close-up photo of a dish"]},
    "document": {"zh": "文档/屏幕", "prompts": ["a photo of a document or screen", "a screenshot or scanned paper"]},
    "other": {"zh": "其他", "prompts": ["a photo of something else", "an abstract image", "a 3d render wallpaper"]},
}

TOP_K = 3
MIN_PROB = 0.15


def status() -> dict:
    reason = ""
    device = "cpu"
    available = True
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except Exception as error:  # noqa: BLE001
        return {
            "available": False,
            "reason": f"缺少 torch/transformers：{error.__class__.__name__}",
            "device": "none",
            "model": DEFAULT_MODEL,
        }
    try:
        import torch

        if torch.cuda.is_available():
            device = "cuda"
            name = torch.cuda.get_device_name(0)
        else:
            name = "cpu"
    except Exception:  # noqa: BLE001
        name = "cpu"
    return {
        "available": available,
        "reason": reason,
        "device": device,
        "device_name": name,
        "model": os.environ.get("PHOTOPLUS_CLIP_MODEL", DEFAULT_MODEL),
        "labels": {key: value["zh"] for key, value in LABELS.items()},
    }


def _text_features(model, tokenizer_output):
    """文本向量：pooler 后再过投影层。

    transformers 5 的 CLIPModel.get_text_features() 直接返回未投影的
    BaseModelOutputWithPooling（512 维但不是共享空间），和图像向量点积会得到
    近乎随机的标签。这里显式走 text_model -> text_projection。
    """
    out = model.text_model(input_ids=tokenizer_output)
    return model.text_projection(out.pooler_output)


def _image_features(model, pixel_values):
    out = model.vision_model(pixel_values=pixel_values)
    return model.visual_projection(out.pooler_output)


class Tagger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._processor = None
        self._text = None
        self._torch = None
        self._error = ""
        self.device = "cpu"

    @property
    def ok(self) -> bool:
        return self._model is not None

    @property
    def error(self) -> str:
        return self._error

    def _load(self) -> bool:
        if self._model is not None:
            return True
        if self._error:
            return False
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
        except Exception as error:  # noqa: BLE001
            self._error = f"缺少 torch/transformers：{error.__class__.__name__}"
            return False

        model_id = os.environ.get("PHOTOPLUS_CLIP_MODEL", DEFAULT_MODEL)
        allow_download = os.environ.get("PHOTOPLUS_CLIP_ALLOW_DOWNLOAD") == "1"
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self._load_on(model_id, allow_download, torch)
        except Exception as error:  # noqa: BLE001
            if self.device == "cuda":
                # 驱动版本过旧、显存不足或 fp16 不被支持时退回 CPU，而不是整块功能不可用。
                self.device = "cpu"
                try:
                    self._load_on(model_id, allow_download, torch)
                except Exception as retry_error:  # noqa: BLE001
                    self._error = f"模型未就绪（{model_id}）：{retry_error.__class__.__name__}"
                    self._model = None
                    return False
            else:
                self._error = f"模型未就绪（{model_id}）：{error.__class__.__name__}"
                self._model = None
                return False

        prompts: list[str] = []
        owners: list[str] = []
        for key, value in LABELS.items():
            for prompt in value["prompts"]:
                prompts.append(prompt)
                owners.append(key)
        tokens = self._processor(text=prompts, return_tensors="pt", padding=True)["input_ids"]
        with torch.inference_mode():
            embeds = _text_features(self._model, tokens.to(self.device))
            embeds = embeds / embeds.norm(dim=-1, keepdim=True)

        # 先按类别平均提示词向量，再在类别之间做 softmax。直接对每条提示词做
        # softmax 会让某一条措辞异常自信，出现“航母 → 家庭 0.81”这类结果。
        classes = []
        for key in LABELS:
            rows = [index for index, owner in enumerate(owners) if owner == key]
            center = embeds[rows].mean(dim=0)
            classes.append(center / center.norm())
        self._text = torch.stack(classes).float()
        self._owners = list(LABELS.keys())
        self._torch = torch
        return True

    def _load_on(self, model_id: str, allow_download: bool, torch) -> None:
        from transformers import CLIPModel, CLIPProcessor

        dtype = torch.float16 if self.device == "cuda" else torch.float32
        self._processor = CLIPProcessor.from_pretrained(
            model_id, local_files_only=not allow_download
        )
        self._model = CLIPModel.from_pretrained(
            model_id, torch_dtype=dtype, local_files_only=not allow_download
        ).to(self.device).eval()

    def tag(self, image) -> list[dict]:
        """返回按概率降序的标签（最多 TOP_K 个，低于阈值丢弃）。"""
        with self._lock:
            if not self._load():
                return []
            torch = self._torch
            inputs = self._processor(images=image, return_tensors="pt")["pixel_values"]
            with torch.inference_mode():
                embeds = _image_features(self._model, inputs.to(self.device))
                embeds = embeds / embeds.norm(dim=-1, keepdim=True)
                logits = self._model.logit_scale.exp() * (embeds.float() @ self._text.T)
                probs = logits.softmax(dim=-1)[0]

        ranked = sorted(
            ((key, float(probs[index])) for index, key in enumerate(self._owners)),
            key=lambda item: item[1],
            reverse=True,
        )
        # "other" 只作为拒识类参与 softmax，不写成 AI/other 标签：
        # 它最大的时候说明这张图不属于任何已知类别，宁可不打标签。
        return [
            {"key": key, "zh": LABELS[key]["zh"], "p": round(prob, 3)}
            for key, prob in ranked
            if key != "other" and prob >= MIN_PROB
        ][:TOP_K]


_tagger: Tagger | None = None
_tagger_lock = threading.Lock()


def get_tagger() -> Tagger:
    global _tagger
    with _tagger_lock:
        if _tagger is None:
            _tagger = Tagger()
        return _tagger
