"""Packaged PDFium for new references; preserve legacy renderer identity and hashes."""
from pathlib import Path
import importlib.util
import importlib.metadata
import shutil
import subprocess
import threading

_LOCK = threading.Lock()  # PDFium's native library is not thread safe.


def pdf_renderer_available() -> bool:
    """Report whether the bundled native renderer can actually be imported."""
    try:
        import pypdfium2  # noqa: F401 - loads the native PDFium library
    except Exception:
        return False
    return True


def new_reference_method() -> str:
    if importlib.util.find_spec("pypdfium2") is not None:
        return "pdfium-" + importlib.metadata.version("pypdfium2")
    return "pdftoppm_150dpi" if shutil.which("pdftoppm") else "unavailable"


def render_pdf_page(pdf: Path, number: int, dpi: int, output: Path, *, method: str) -> None:
    if number < 1 or not 36 <= dpi <= 1200:
        raise ValueError("invalid_pdf_render_parameters")
    if method.startswith("pdfium-"):
        import pypdfium2 as pdfium
        if method != "pdfium-" + importlib.metadata.version("pypdfium2"):
            raise ValueError("pdf_renderer_version_mismatch")
        with _LOCK:
            doc = pdfium.PdfDocument(str(pdf))
            try:
                page = doc[number - 1]
                try:
                    width, height = page.get_size()
                    scaled_width, scaled_height = width * dpi / 72, height * dpi / 72
                    if not (0 < scaled_width <= 12000 and 0 < scaled_height <= 12000 and scaled_width * scaled_height <= 40_000_000):
                        raise ValueError("pdf_render_dimensions_exceeded")
                    bitmap = page.render(scale=dpi / 72)
                    try:
                        image = bitmap.to_pil()
                        try:
                            image.save(output, format="PNG")
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                doc.close()
    else:
        executable = shutil.which("pdftoppm")
        if not executable:
            raise FileNotFoundError("pdf_renderer_unavailable")
        subprocess.run([executable, "-f", str(number), "-l", str(number), "-r", str(dpi),
                        "-png", "-singlefile", str(pdf), str(output.with_suffix(""))],
                       check=True, capture_output=True, timeout=90)
