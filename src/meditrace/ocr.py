"""OCR for scanned reports and prescriptions (PNG/JPEG images and image-only
PDFs), using the Tesseract binary through pytesseract.

Optional by design: the API image stays small and Tesseract only has to be
installed where the extraction worker runs (``pip install -r
requirements-ocr.txt`` plus ``apt install tesseract-ocr``). Without it,
extraction of a scanned source fails with a clear error instead of silently
producing no facts.

Returned text keeps one line per OCR line and a form feed between pages, and
every line's mean word confidence is kept so the extractor can carry OCR
certainty into each fact's confidence and evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import shutil


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class OCRResult:
    text: str
    engine: str
    # (page, line_number_within_page) -> mean word confidence 0..1
    line_confidence: dict[tuple[int, int], float] = field(default_factory=dict)


def _require_tesseract():
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise OCRUnavailable(
            "OCR needs `pip install -r requirements-ocr.txt` on the worker"
        ) from exc
    if not shutil.which("tesseract"):
        raise OCRUnavailable("OCR needs the Tesseract binary (apt install tesseract-ocr)")
    return pytesseract


def engine_name() -> str:
    pytesseract = _require_tesseract()
    return f"tesseract {pytesseract.get_tesseract_version()}"


def _ocr_page(image, page: int, result: OCRResult) -> list[str]:
    pytesseract = _require_tesseract()
    image = image.convert("L")
    if image.width < 1400:  # small scans OCR much better upscaled
        scale = 1400 / image.width
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    data = pytesseract.image_to_data(
        image, config="--psm 6", output_type=pytesseract.Output.DICT
    )
    lines: dict[tuple[int, int, int], list[tuple[str, float]]] = {}
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        conf = float(data["conf"][i])
        lines.setdefault(key, []).append((word, max(conf, 0.0) / 100.0))
    out: list[str] = []
    for number, key in enumerate(sorted(lines), 1):
        words = lines[key]
        out.append(" ".join(w for w, _ in words))
        result.line_confidence[(page, number)] = round(
            sum(c for _, c in words) / len(words), 4
        )
    return out


def ocr_image(content: bytes) -> OCRResult:
    from PIL import Image

    result = OCRResult(text="", engine=engine_name())
    with Image.open(io.BytesIO(content)) as image:
        result.text = "\n".join(_ocr_page(image, 1, result))
    return result


def ocr_pdf(content: bytes, dpi: int = 300) -> OCRResult:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise OCRUnavailable("Scanned-PDF OCR needs pypdfium2 (requirements-ocr.txt)") from exc
    result = OCRResult(text="", engine=engine_name())
    pages: list[str] = []
    pdf = pdfium.PdfDocument(content)
    try:
        for index in range(len(pdf)):
            image = pdf[index].render(scale=dpi / 72).to_pil()
            pages.append("\n".join(_ocr_page(image, index + 1, result)))
    finally:
        pdf.close()
    result.text = "\f".join(pages)
    return result
