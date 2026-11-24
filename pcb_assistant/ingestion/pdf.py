"""PDF text, image and render helpers on permissively licensed libraries.

PyMuPDF is AGPL-3.0 (or a paid commercial licence), so a permissively
licensed distribution cannot ship it. Page text, page geometry and embedded
images come from pypdf (BSD-3-Clause); page rendering uses pypdfium2
(Apache-2.0). Nothing leaves the machine and the caller stays responsible for
size, page and character limits.
"""
from __future__ import annotations

import io


class PdfError(Exception):
    """A PDF could not be read safely."""


class EncryptedPdf(PdfError):
    """The PDF needs a password; callers must not guess one."""


class PdfDocument:
    """Read-only PDF view with text, geometry, images and page renders."""

    def __init__(self, data):
        import pypdf
        try:
            reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        except Exception as exc:  # malformed or unsupported container
            raise PdfError('pdf_read_failed') from exc
        if getattr(reader, 'is_encrypted', False):
            raise EncryptedPdf('encrypted_pdf')
        self._reader = reader
        self._data = data
        self._pdfium = None

    def __len__(self):
        return len(self._reader.pages)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        if self._pdfium is not None:
            try:
                self._pdfium.close()
            finally:
                self._pdfium = None

    def _page(self, index):
        try:
            return self._reader.pages[index]
        except Exception as exc:
            raise PdfError('pdf_page_unreadable') from exc

    def page_text(self, index):
        try:
            return self._page(index).extract_text() or ''
        except PdfError:
            raise
        except Exception as exc:
            raise PdfError('pdf_text_extraction_failed') from exc

    def page_size(self, index):
        box = self._page(index).mediabox
        try:
            return float(box.width), float(box.height)
        except Exception:
            return 0.0, 0.0

    def page_images(self, index):
        """Return (image bytes, decode errors) for one page.

        An image whose filter pypdf cannot decode is skipped and counted so
        the caller can keep the readable rest of the page.
        """
        try:
            images = list(self._page(index).images)
        except Exception as exc:
            raise PdfError('pdf_image_extraction_failed') from exc
        payloads = []
        errors = 0
        for item in images:
            try:
                payloads.append(item.data)
            except Exception:
                errors += 1
        return payloads, errors

    def _pdfium_page(self, index):
        import pypdfium2
        if self._pdfium is None:
            try:
                self._pdfium = pypdfium2.PdfDocument(self._data)
            except Exception as exc:
                raise PdfError('pdf_render_open_failed') from exc
        try:
            return self._pdfium[index]
        except Exception as exc:
            raise PdfError('pdf_render_page_failed') from exc

    def render_page(self, index, scale=2.0):
        """Render one page to an RGB Pillow image."""
        page = self._pdfium_page(index)
        try:
            bitmap = page.render(scale=max(0.1, float(scale)))
        except Exception as exc:
            raise PdfError('pdf_render_failed') from exc
        try:
            return bitmap.to_pil().convert('RGB')
        except Exception as exc:
            raise PdfError('pdf_render_failed') from exc
        finally:
            bitmap.close()


def open_document(data):
    """Open PDF bytes; raises EncryptedPdf or PdfError."""
    return PdfDocument(data)
