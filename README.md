# a4-printer-interface

`a4-printer-interface` is a focused Python library for discovering local printer
queues and printing PDF documents or images on Windows, X11, and macOS.

Every input page is normalized for a portrait A4 sheet before it is submitted:

- Landscape pages are rotated to portrait.
- Pages of any size are scaled proportionally to fit A4.
- Pages whose aspect ratio differs from A4 are centered with white padding.
- Color and true grayscale rendering are supported.

The source PDF is never modified. PDFium renders each page at 300 DPI so that
layout and grayscale results do not depend on a desktop PDF viewer.

Images are EXIF-orientation aware and become a temporary, single-page portrait A4
PDF before printing. Landscape images are rotated, then the image is scaled and
center-cropped to cover the A4 canvas without an added white border. Multi-frame
images use their first frame.

## Requirements

- Python 3.10 or newer.
- A printer configured in Windows, CUPS on X11, or the macOS printing system.
- The CUPS client commands `lpstat` and `lp` on X11. They are built into macOS.
- An A4-capable printer queue.

The lightweight runtime uses `pypdfium2`, Pillow, and ReportLab. Pillow-supported
image formats such as PNG, JPEG, TIFF, BMP, and WebP can be printed. Windows also
installs `pywin32` for direct GDI printing. The package does not install a GUI
toolkit or require an external PDF viewer.

## Installation

From the repository root:

```console
python -m pip install a4_printer_interface
```

## Discover printers

```python
from a4_printer_interface import PrinterManager

manager = PrinterManager()
for printer in manager.list_printers():
    print(printer.get_name(), printer.get_uuid())
```

Names are matched case-insensitively. UUIDs are deterministic identifiers derived
from the operating-system queue and its backend identity.

```python
office_queues = manager.get_printer_by_name("Office Printer")
printer = manager.get_printer_by_uuid("8fd0d51f-12c8-5b50-a2f1-4f64641ae77c")
```

Create a new `PrinterManager` when you need to refresh the operating system's
printer list.

## Check printer activity

Use `get_busy()` to check whether the selected queue is currently executing a
print job:

```python
if printer.get_busy():
    print("The printer is currently printing.")
```

The method returns `False` for an idle printer or for jobs that are only queued,
paused, completed, or waiting for intervention. It raises `RuntimeError` when the
native print service cannot provide the queue status.

## Print a document

Use the built-in `DefaultSettings` for color printing:

```python
from a4_printer_interface import DefaultSettings, PrinterManager

printer = PrinterManager().list_printers()[0]
printer.print("report.pdf", DefaultSettings())
```

Use the built-in `GrayscaleSettings` for grayscale printing:

```python
from a4_printer_interface import GrayscaleSettings

printer.print("report.pdf", GrayscaleSettings())
```

Images use the same API and settings:

```python
printer.print("photo.jpg", DefaultSettings())
```

In grayscale mode, PDFium renders the document directly to grayscale pixels before
it reaches the driver. The native print job is also marked as monochrome. In color
mode, the original page colors are preserved.

`print()` rejects files that are neither PDFs nor Pillow-supported images,
malformed or password-protected PDFs, missing files, unavailable queues, and jobs
rejected by the native print system. A successful return means the operating
system accepted the job; it does not mean that physical printing has finished.

## Print the built-in test page

The package includes a low-ink, single-page color test PDF with small cyan,
magenta, yellow, and black patches, a black cross grid, and English and Chinese
sample text. Print it in color with the default settings:

```python
printer.print_test_page()
```

The optional settings argument can force a grayscale test:

```python
printer.print_test_page(GrayscaleSettings())
```

## Platform backends

- Windows discovers queues with the print spooler and sends normalized A4 page
  images directly to the selected printer through GDI. Printer defaults are not
  changed.
- X11 and macOS discover CUPS queues with `lpstat`. They receive a temporary,
  normalized A4 PDF through `lp`; the temporary file is deleted after submission.

## Command line

List available queues after installation:

```console
a4-printers
```

The equivalent development command is:

```console
python -m a4_printer_interface
```

## Development

Run the test suite:

```console
python -m unittest discover -s tests -v
```

Tests inspect normalized PDF page sizes and rendered pixels. Operating-system
printer services are mocked, so the tests never submit a real print job. CI runs
them on Windows, Ubuntu, and macOS with Python 3.10 and 3.13.

## License

MIT
