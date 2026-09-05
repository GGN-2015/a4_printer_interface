"""Cross-platform A4 PDF and image printing without a GUI framework."""

from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import sys
import uuid as uuid_module
from enum import Enum
from importlib.resources import as_file, files
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, List, Optional

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


class ColorMode(Enum):
    COLOR = "color"
    GREY_SCALE = "grey_scale"


class PrintSettings:
    def __init__(self):
        self._color_mode = ColorMode.COLOR

    def get_color_mode(self) -> ColorMode:
        return self._color_mode


class DefaultSettings(PrintSettings):
    def get_color_mode(self) -> ColorMode:
        return ColorMode.COLOR


class GrayscaleSettings(PrintSettings):
    def get_color_mode(self) -> ColorMode:
        return ColorMode.GREY_SCALE


class PrinterInterface:
    def __init__(self):
        self._name: Optional[str] = None
        self._uuid: Optional[str] = None
        self._backend: Optional[str] = None

    def get_name(self) -> str:
        if self._name is None:
            raise RuntimeError(
                "This printer is not connected to an operating-system queue"
            )
        return self._name

    def get_uuid(self) -> str:
        if self._uuid is None:
            raise RuntimeError(
                "This printer is not connected to an operating-system queue"
            )
        return self._uuid

    def print(
        self, document_path: str, settings: Optional[PrintSettings] = None
    ) -> None:
        if settings is None:
            settings = DefaultSettings()

        if not isinstance(settings, PrintSettings):
            raise TypeError("settings must be a PrintSettings instance")

        color_mode = settings.get_color_mode()
        if not isinstance(color_mode, ColorMode):
            raise ValueError("PrintSettings.get_color_mode() must return a ColorMode")

        path = Path(document_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Document does not exist or is not a file: {path}")
        if self._backend is None:
            raise RuntimeError(
                "This printer is not connected to a supported print backend"
            )

        _print_document(self._backend, self.get_name(), path.resolve(), color_mode)

    def print_test_page(self, settings: Optional[PrintSettings] = None) -> None:
        if settings is None:
            settings = DefaultSettings()
        resource = files("a4_printer_interface").joinpath(
            os.path.join("assets", "printer_test_page.pdf")
        )
        with as_file(resource) as test_page:
            self.print(str(test_page), settings)

    def get_busy(self) -> bool:
        if self._backend is None:
            raise RuntimeError(
                "This printer is not connected to a supported print backend"
            )
        return _get_printer_busy(self._backend, self.get_name())


class PrinterManager:
    def __init__(self):
        self._printers = _discover_printers()

    def list_printers(self) -> List[PrinterInterface]:
        return list(self._printers)

    def get_printer_by_name(self, name: str) -> List[PrinterInterface]:
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        target = name.casefold()
        return [
            printer
            for printer in self._printers
            if printer.get_name().casefold() == target
        ]

    def get_printer_by_uuid(self, uuid: str) -> Optional[PrinterInterface]:
        if not isinstance(uuid, str):
            raise TypeError("uuid must be a string")
        target = uuid.casefold()
        return next(
            (
                printer
                for printer in self._printers
                if printer.get_uuid().casefold() == target
            ),
            None,
        )


_RENDER_DPI = 300
_A4_PIXELS = (
    round(210 / 25.4 * _RENDER_DPI),
    round(297 / 25.4 * _RENDER_DPI),
)


def _backend_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin" or os.name == "posix":
        return "cups"
    raise RuntimeError(f"Unsupported operating system: {sys.platform}")


def _make_printer(name: str, identity: str, backend: str) -> PrinterInterface:
    printer = PrinterInterface()
    printer._name = name
    printer._uuid = str(
        uuid_module.uuid5(
            uuid_module.NAMESPACE_URL,
            f"a4-printer-interface:{backend}:{identity}",
        )
    )
    printer._backend = backend
    return printer


def _discover_printers() -> List[PrinterInterface]:
    backend = _backend_name()
    if backend == "windows":
        return _discover_windows_printers()
    return _discover_cups_printers()


def _windows_printer_records() -> list[dict[str, object]]:
    try:
        import win32print

        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return list(win32print.EnumPrinters(flags, None, 2))
    except ImportError as error:
        raise RuntimeError(
            "pywin32 is required by the Windows print backend"
        ) from error
    except Exception as error:
        raise RuntimeError(f"Could not discover Windows printers: {error}") from error


def _discover_windows_printers() -> List[PrinterInterface]:
    printers = []
    for record in _windows_printer_records():
        name = str(record.get("pPrinterName") or "").strip()
        if not name:
            continue
        identity = "\0".join(
            str(record.get(field) or "")
            for field in ("pServerName", "pPrinterName", "pPortName", "pDriverName")
        )
        printers.append(_make_printer(name, identity, "windows"))
    return sorted(printers, key=lambda printer: printer.get_name().casefold())


def _windows_printer_busy(name: str) -> bool:
    try:
        import win32print
    except ImportError as error:
        raise RuntimeError(
            "pywin32 is required by the Windows print backend"
        ) from error

    handle = None
    try:
        handle = win32print.OpenPrinter(name)
        printer = win32print.GetPrinter(handle, 2)
        printer_status = int(printer.get("Status") or 0)
        if printer_status & win32print.PRINTER_STATUS_PRINTING:
            return True

        job_count = int(printer.get("cJobs") or 0)
        jobs = win32print.EnumJobs(handle, 0, job_count, 1)
        return any(
            int(job.get("Status") or 0) & win32print.JOB_STATUS_PRINTING for job in jobs
        )
    except Exception as error:
        raise RuntimeError(
            f"Could not query Windows printer status for {name}: {error}"
        ) from error
    finally:
        if handle is not None:
            win32print.ClosePrinter(handle)


def _run_command(
    command: List[str], *, env: Optional[dict[str, str]] = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Could not run {command[0]}: {error}") from error


def _cups_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    return environment


def _cups_printer_busy(name: str) -> bool:
    lpstat = shutil.which("lpstat")
    if lpstat is None:
        raise RuntimeError("CUPS is required, but the lpstat command was not found")

    result = _run_command([lpstat, "-p", name], env=_cups_environment())
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown CUPS error"
        raise RuntimeError(f"Could not query CUPS printer status: {detail}")

    printing_pattern = re.compile(
        rf"^printer\s+{re.escape(name)}\s+(?:now|is)\s+printing\b",
        re.IGNORECASE,
    )
    return any(
        printing_pattern.search(line.strip()) for line in result.stdout.splitlines()
    )


def _get_printer_busy(backend: str, name: str) -> bool:
    if backend == "windows":
        return _windows_printer_busy(name)
    if backend == "cups":
        return _cups_printer_busy(name)
    raise RuntimeError(f"Unsupported print backend: {backend}")


def _discover_cups_printers() -> List[PrinterInterface]:
    lpstat = shutil.which("lpstat")
    if lpstat is None:
        raise RuntimeError("CUPS is required, but the lpstat command was not found")

    environment = _cups_environment()
    names_result = _run_command([lpstat, "-e"], env=environment)
    names = [line.strip() for line in names_result.stdout.splitlines() if line.strip()]

    if names_result.returncode != 0 or not names:
        legacy_result = _run_command([lpstat, "-p"], env=environment)
        names = []
        if legacy_result.returncode == 0:
            for line in legacy_result.stdout.splitlines():
                match = re.match(r"^printer\s+(\S+)", line)
                if match:
                    names.append(match.group(1))

    devices_result = _run_command([lpstat, "-v"], env=environment)
    devices = {}
    if devices_result.returncode == 0:
        for line in devices_result.stdout.splitlines():
            match = re.match(r"^device for (.+?):\s*(.+)$", line)
            if match:
                devices[match.group(1)] = match.group(2).strip()

    printers = [
        _make_printer(name, f"{name}\0{devices.get(name, '')}", "cups")
        for name in dict.fromkeys(names)
    ]
    return sorted(printers, key=lambda printer: printer.get_name().casefold())


def _open_pdf(path: Path) -> pdfium.PdfDocument:
    try:
        document = pdfium.PdfDocument(str(path))
        document.init_forms()
        if len(document) == 0:
            document.close()
            raise ValueError(f"PDF document has no pages: {path}")
        return document
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Could not load PDF document: {path} ({error})") from error


def _render_pdf_a4_pages(path: Path, color_mode: ColorMode) -> Iterator[Image.Image]:
    document = _open_pdf(path)
    output_mode = "RGB" if color_mode is ColorMode.COLOR else "L"
    try:
        for page_number in range(len(document)):
            page = document[page_number]
            try:
                try:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise ValueError(
                            f"PDF page {page_number + 1} has an invalid size"
                        )

                    rotation = 90 if width > height else 0
                    if rotation:
                        width, height = height, width
                    scale = min(_A4_PIXELS[0] / width, _A4_PIXELS[1] / height)
                    bitmap = page.render(
                        scale=scale,
                        rotation=rotation,
                        fill_color=(255, 255, 255, 255),
                        grayscale=color_mode is ColorMode.GREY_SCALE,
                        optimize_mode="print",
                        draw_annots=True,
                    )
                    try:
                        pil_image = bitmap.to_pil()
                        try:
                            rendered = pil_image.convert(output_mode).copy()
                        finally:
                            pil_image.close()
                    finally:
                        bitmap.close()

                    if (
                        rendered.width > _A4_PIXELS[0]
                        or rendered.height > _A4_PIXELS[1]
                    ):
                        rendered.thumbnail(_A4_PIXELS, Image.Resampling.LANCZOS)
                    a4_page = Image.new(output_mode, _A4_PIXELS, "white")
                    offset = (
                        (_A4_PIXELS[0] - rendered.width) // 2,
                        (_A4_PIXELS[1] - rendered.height) // 2,
                    )
                    a4_page.paste(rendered, offset)
                    rendered.close()
                    yield a4_page
                except ValueError:
                    raise
                except Exception as error:
                    raise ValueError(
                        f"Could not render PDF page {page_number + 1}: {error}"
                    ) from error
            finally:
                page.close()
    finally:
        document.close()


def _render_image_a4_page(path: Path, color_mode: ColorMode) -> Image.Image:
    output_mode = "RGB" if color_mode is ColorMode.COLOR else "L"
    try:
        with Image.open(path) as source:
            source.seek(0)
            transposed = ImageOps.exif_transpose(source)
            try:
                transposed.load()
                if transposed.width <= 0 or transposed.height <= 0:
                    raise ValueError(f"Image has an invalid size: {path}")

                if (
                    transposed.mode in {"RGBA", "LA"}
                    or "transparency" in transposed.info
                ):
                    rgba = transposed.convert("RGBA")
                    try:
                        background = Image.new("RGBA", rgba.size, "white")
                        background.alpha_composite(rgba)
                        prepared = background.convert(output_mode)
                        background.close()
                    finally:
                        rgba.close()
                else:
                    prepared = transposed.convert(output_mode)
            finally:
                if transposed is not source:
                    transposed.close()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(
            f"Document is neither a PDF nor a supported image: {path}"
        ) from error

    try:
        if prepared.width > prepared.height:
            portrait = prepared.transpose(Image.Transpose.ROTATE_90)
            prepared.close()
            prepared = portrait
        return ImageOps.fit(
            prepared,
            _A4_PIXELS,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    finally:
        prepared.close()


def _render_a4_pages(path: Path, color_mode: ColorMode) -> Iterator[Image.Image]:
    if path.suffix.casefold() == ".pdf":
        yield from _render_pdf_a4_pages(path, color_mode)
        return
    yield _render_image_a4_page(path, color_mode)


def _write_normalized_pdf(path: Path, output_path: Path, color_mode: ColorMode) -> None:
    pages = iter(_render_a4_pages(path, color_mode))
    first_page = next(pages)
    output = canvas.Canvas(str(output_path), pagesize=A4, pageCompression=1)
    first_page_pending = True
    try:
        for page in itertools.chain((first_page,), pages):
            encoded = BytesIO()
            try:
                page.save(
                    encoded,
                    format="JPEG",
                    quality=95,
                    subsampling=0,
                    dpi=(_RENDER_DPI, _RENDER_DPI),
                )
                encoded.seek(0)
                output.drawImage(ImageReader(encoded), 0, 0, width=A4[0], height=A4[1])
                output.showPage()
            finally:
                page.close()
                if page is first_page:
                    first_page_pending = False
                encoded.close()
        output.save()
    finally:
        if first_page_pending:
            first_page.close()
        pages.close()


def _print_document(backend: str, name: str, path: Path, color_mode: ColorMode) -> None:
    if backend not in {"windows", "cups"}:
        raise RuntimeError(f"Unsupported print backend: {backend}")
    if backend == "windows" and path.suffix.casefold() == ".pdf":
        _print_with_windows(name, path, color_mode)
        return

    with TemporaryDirectory(prefix="a4-printer-interface-") as directory:
        normalized_path = Path(directory) / "print-ready.pdf"
        _write_normalized_pdf(path, normalized_path, color_mode)
        if backend == "windows":
            _print_with_windows(name, normalized_path, color_mode)
        else:
            _print_with_cups(name, normalized_path, color_mode)


def _print_with_cups(name: str, path: Path, color_mode: ColorMode) -> None:
    lp = shutil.which("lp")
    if lp is None:
        raise RuntimeError("CUPS is required, but the lp command was not found")

    ipp_color_mode = "color" if color_mode is ColorMode.COLOR else "monochrome"
    result = _run_command(
        [
            lp,
            "-d",
            name,
            "-o",
            "media=A4",
            "-o",
            "orientation-requested=3",
            "-o",
            f"print-color-mode={ipp_color_mode}",
            str(path),
        ],
        env=_cups_environment(),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown CUPS error"
        raise RuntimeError(f"CUPS rejected the print job: {detail}")


def _windows_devmode(name: str, color_mode: ColorMode):
    import win32con
    import win32print

    handle = win32print.OpenPrinter(name)
    try:
        devmode = win32print.GetPrinter(handle, 2).get("pDevMode")
        if devmode is None:
            raise RuntimeError(f"Printer driver did not provide settings: {name}")
        devmode.Fields |= (
            win32con.DM_ORIENTATION | win32con.DM_PAPERSIZE | win32con.DM_COLOR
        )
        devmode.Orientation = win32con.DMORIENT_PORTRAIT
        devmode.PaperSize = win32con.DMPAPER_A4
        devmode.Color = (
            win32con.DMCOLOR_COLOR
            if color_mode is ColorMode.COLOR
            else win32con.DMCOLOR_MONOCHROME
        )
        return devmode
    finally:
        win32print.ClosePrinter(handle)


def _fit_device_rect(
    image_size: tuple[int, int], device_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    scale = min(device_size[0] / image_size[0], device_size[1] / image_size[1])
    width = round(image_size[0] * scale)
    height = round(image_size[1] * scale)
    left = (device_size[0] - width) // 2
    top = (device_size[1] - height) // 2
    return left, top, left + width, top + height


def _print_with_windows(name: str, path: Path, color_mode: ColorMode) -> None:
    try:
        import win32con
        import win32gui
        import win32ui
        from PIL import ImageWin
    except ImportError as error:
        raise RuntimeError(
            "pywin32 is required by the Windows print backend"
        ) from error

    pages = iter(_render_a4_pages(path, color_mode))
    first_page = next(pages)
    first_page_pending = True
    device_context = None
    document_started = False
    try:
        devmode = _windows_devmode(name, color_mode)
        handle = win32gui.CreateDC("WINSPOOL", name, devmode)
        device_context = win32ui.CreateDCFromHandle(handle)
        printable_size = (
            device_context.GetDeviceCaps(win32con.HORZRES),
            device_context.GetDeviceCaps(win32con.VERTRES),
        )
        target = _fit_device_rect(_A4_PIXELS, printable_size)

        device_context.StartDoc(path.name)
        document_started = True
        for page in itertools.chain((first_page,), pages):
            try:
                device_context.StartPage()
                ImageWin.Dib(page).draw(device_context.GetHandleOutput(), target)
                device_context.EndPage()
            finally:
                page.close()
                if page is first_page:
                    first_page_pending = False
        device_context.EndDoc()
        document_started = False
    except (ValueError, RuntimeError):
        raise
    except Exception as error:
        raise RuntimeError(f"Windows rejected the print job: {error}") from error
    finally:
        if document_started and device_context is not None:
            try:
                device_context.AbortDoc()
            except Exception:
                pass
        if device_context is not None:
            device_context.DeleteDC()
        if first_page_pending:
            first_page.close()
        pages.close()


def main() -> int:
    try:
        printers = PrinterManager().list_printers()
    except RuntimeError as error:
        print(f"Printer discovery failed: {error}", file=sys.stderr)
        return 1

    if not printers:
        print("No printers found.")
        return 0
    for printer in printers:
        print(f"Printer Name: {printer.get_name()}, UUID: {printer.get_uuid()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
