"""Phase 6 — chest X-ray inference wiring.

No pretrained clinical checkpoint ships with this repo — that would be an
unverified claim of clinical performance. ``CXR_MODEL_PATH`` must point to a
checkpoint produced by ``notebooks/CXR_Sentinel_Full.ipynb`` (or
``src/train.py``) against real, license-compliant chest X-ray data. Without
one, this module reports unavailable rather than fabricating a result.
Requires the optional ``requirements-ml.txt`` (torch/torchvision); those are
not part of the default API deployment.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os

TARGET_FINDINGS = ["cardiomegaly", "pleural_effusion", "lung_opacity"]


@dataclass
class CXRResult:
    findings: dict[str, float]
    model_version: str
    checkpoint_sha256: str


class CXRUnavailable(RuntimeError):
    pass


def _checkpoint_path() -> str:
    path = os.getenv("CXR_MODEL_PATH")
    if not path or not os.path.isfile(path):
        raise CXRUnavailable(
            "CXR_MODEL_PATH is not set to an existing checkpoint. Train one with "
            "notebooks/CXR_Sentinel_Full.ipynb, then set CXR_MODEL_PATH and deploy "
            "the ml worker (requirements-ml.txt) to enable this endpoint."
        )
    return path


_model_cache: dict[str, object] = {}


def _load_model(path: str):
    if path in _model_cache:
        return _model_cache[path]
    try:
        import torch
    except ImportError as exc:
        raise CXRUnavailable(
            "torch/torchvision are not installed. Deploy the ml worker with "
            "requirements-ml.txt to run CXR inference."
        ) from exc
    from src.model import CXRClassifier

    model = CXRClassifier(num_targets=len(TARGET_FINDINGS), pretrained=False)
    state = torch.load(path, map_location="cpu")
    model.load_state_dict(state.get("model_state_dict", state))
    model.eval()
    _model_cache[path] = model
    return model


def analyze(image_bytes: bytes) -> CXRResult:
    """Raises CXRUnavailable when no checkpoint is configured; never fabricates output."""
    path = _checkpoint_path()
    import torch
    from PIL import Image
    from torchvision import transforms

    model = _load_model(path)
    preprocess = transforms.Compose(
        [
            transforms.Grayscale(num_output_channels=3),
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    tensor = preprocess(image).unsqueeze(0)
    with torch.no_grad():
        logits = model(tensor)
        probabilities = torch.sigmoid(logits).squeeze(0).tolist()
    with open(path, "rb") as checkpoint_file:
        checkpoint_hash = hashlib.sha256(checkpoint_file.read()).hexdigest()
    return CXRResult(
        findings=dict(zip(TARGET_FINDINGS, (round(p, 4) for p in probabilities))),
        model_version=os.path.basename(path),
        checkpoint_sha256=checkpoint_hash,
    )
