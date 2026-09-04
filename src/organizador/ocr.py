"""Optional OCR for image-only PDF pages using the built-in Windows engine.

Everything in this module degrades gracefully: missing packages, a missing
OCR language pack, or any recognition failure simply yields no text, and the
caller keeps its filename-only fallback. Nothing here may raise for
environmental reasons; programming errors (bad arguments) still raise.
"""

from __future__ import annotations

import asyncio
import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

MAX_OCR_PAGES = 40
RENDER_SCALE = 2.0

LANGUAGE_TAGS: dict[str, tuple[str, ...]] = {
    "pt": ("pt-PT", "pt-BR"),
    "en": ("en-US", "en-GB"),
    "es": ("es-ES",),
    "fr": ("fr-FR",),
}


def _import_winrt() -> dict[str, Any] | None:
    """Import the WinRT projection modules, or return ``None`` when absent."""

    try:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    except ImportError:
        return None
    return {
        "Language": Language,
        "BitmapDecoder": BitmapDecoder,
        "OcrEngine": OcrEngine,
        "DataWriter": DataWriter,
        "InMemoryRandomAccessStream": InMemoryRandomAccessStream,
    }


@lru_cache(maxsize=1)
def _winrt() -> dict[str, Any] | None:
    return _import_winrt()


@lru_cache(maxsize=1)
def _pdfium() -> Any | None:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    return pdfium


def preferred_language_tags(app_language: str) -> tuple[str, ...]:
    """Return OCR language tags to try, in order, for an app language code."""

    return (*LANGUAGE_TAGS.get(app_language, ()), "en-US")


def available_languages() -> tuple[str, ...]:
    """Return installed OCR language tags, or an empty tuple when unavailable."""

    modules = _winrt()
    if modules is None:
        return ()
    try:
        return tuple(
            sorted(
                {
                    language.language_tag
                    for language in modules["OcrEngine"].available_recognizer_languages
                }
            )
        )
    except Exception:
        LOGGER.debug("Could not list OCR languages", exc_info=True)
        return ()


def ocr_available(language_tags: tuple[str, ...] = ("pt-PT",)) -> bool:
    """Return whether an OCR engine exists for any of the given tags."""

    installed = set(available_languages())
    return any(tag in installed for tag in language_tags)


def _engine_for(tags: tuple[str, ...]) -> Any | None:
    modules = _winrt()
    if modules is None:
        return None
    installed = set(available_languages())
    for tag in tags:
        if tag not in installed:
            continue
        try:
            engine = modules["OcrEngine"].try_create_from_language(modules["Language"](tag))
        except Exception:
            LOGGER.debug("Could not create OCR engine for %s", tag, exc_info=True)
            continue
        if engine is not None:
            return engine
    return None


@lru_cache(maxsize=4)
def _cached_engine(tags: tuple[str, ...]) -> Any | None:
    return _engine_for(tags)


async def _recognize_async(modules: dict[str, Any], engine: Any, image_bytes: bytes) -> str:
    stream = modules["InMemoryRandomAccessStream"]()
    writer = modules["DataWriter"](stream)
    writer.write_bytes(image_bytes)
    await writer.store_async()
    writer.detach_stream()
    decoder = await modules["BitmapDecoder"].create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return str(result.text or "")


def recognize_page(image_bytes: bytes, language_tags: tuple[str, ...]) -> str:
    """Recognize text in one PNG image; return ``""`` on any failure."""

    if not image_bytes:
        return ""
    modules = _winrt()
    engine = _cached_engine(tuple(language_tags)) if modules is not None else None
    if modules is None or engine is None:
        return ""
    try:
        return asyncio.run(_recognize_async(modules, engine, image_bytes))
    except Exception:
        LOGGER.debug("OCR recognition failed", exc_info=True)
        return ""


def render_pdf_pages(
    path: Path, *, max_pages: int = MAX_OCR_PAGES, scale: float = RENDER_SCALE
) -> list[bytes]:
    """Render PDF pages to PNG bytes; return ``[]`` on any failure."""

    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    pdfium = _pdfium()
    if pdfium is None:
        return []
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception:
        LOGGER.debug("Could not open PDF for rendering: %s", path, exc_info=True)
        return []
    rendered: list[bytes] = []
    try:
        for index in range(min(len(document), max_pages)):
            try:
                bitmap = document[index].render(scale=scale)
                image = bitmap.to_pil()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                rendered.append(buffer.getvalue())
            except Exception:
                LOGGER.debug("Could not render page %d of %s", index, path, exc_info=True)
    finally:
        document.close()
    return rendered


def ocr_blank_pages(
    path: Path,
    pages: list[str],
    language_tags: tuple[str, ...],
    *,
    max_pages: int = MAX_OCR_PAGES,
) -> list[str]:
    """Fill blank extracted pages with OCR text, best effort, in place order."""

    blank = [index for index, page in enumerate(pages) if not page.strip()]
    if not blank or max_pages < 1:
        return pages
    if not ocr_available(language_tags):
        return pages
    rendered = {
        index: image for index, image in enumerate(render_pdf_pages(path, max_pages=max_pages))
    }
    filled = list(pages)
    done = 0
    for index in blank:
        if done >= max_pages or index not in rendered:
            break
        text = recognize_page(rendered[index], language_tags).strip()
        done += 1
        if text:
            filled[index] = text
    if done:
        LOGGER.info("OCR recovered %d page(s) in %s", done, path)
    return filled
