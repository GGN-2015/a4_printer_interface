import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pypdfium2 as pdfium
from PIL import Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

import a4_printer_interface.main as printer_module
from a4_printer_interface import (
    ColorMode,
    DefaultSettings,
    GrayscaleSettings,
    PrinterInterface,
    PrinterManager,
    PrintSettings,
)


def completed(command, returncode=0, stdout="", stderr=""):
    return printer_module.subprocess.CompletedProcess(
        command, returncode, stdout, stderr
    )


class PrintSettingsTests(unittest.TestCase):
    def test_base_settings_default_to_color(self):
        self.assertIs(PrintSettings().get_color_mode(), ColorMode.COLOR)

    def test_default_settings_use_color(self):
        settings = DefaultSettings()
        self.assertIsInstance(settings, PrintSettings)
        self.assertIs(settings.get_color_mode(), ColorMode.COLOR)

    def test_grayscale_settings_use_grayscale(self):
        settings = GrayscaleSettings()
        self.assertIsInstance(settings, PrintSettings)
        self.assertIs(settings.get_color_mode(), ColorMode.GREY_SCALE)


class PrinterManagerTests(unittest.TestCase):
    def setUp(self):
        self.office = printer_module._make_printer("Office", "office", "cups")
        self.photo = printer_module._make_printer("Photo", "photo", "cups")

    @mock.patch.object(printer_module, "_discover_printers")
    def test_lists_and_finds_printers(self, discover):
        discover.return_value = [self.office, self.photo]
        manager = PrinterManager()

        self.assertEqual(manager.list_printers(), [self.office, self.photo])
        self.assertEqual(manager.get_printer_by_name("OFFICE"), [self.office])
        self.assertIs(
            manager.get_printer_by_uuid(self.photo.get_uuid().upper()), self.photo
        )
        self.assertIsNone(manager.get_printer_by_uuid("missing"))

    @mock.patch.object(printer_module, "_discover_printers")
    def test_returns_a_copy_of_the_printer_list(self, discover):
        discover.return_value = [self.office]
        manager = PrinterManager()
        manager.list_printers().clear()
        self.assertEqual(manager.list_printers(), [self.office])


class DiscoveryTests(unittest.TestCase):
    @mock.patch.object(printer_module, "_windows_printer_records")
    def test_discovers_windows_printers(self, records):
        records.return_value = [
            {
                "pPrinterName": "Zebra",
                "pServerName": None,
                "pPortName": "USB001",
                "pDriverName": "Zebra Driver",
            },
            {
                "pPrinterName": "Office",
                "pServerName": "server",
                "pPortName": "IP_1",
                "pDriverName": "Office Driver",
            },
        ]

        printers = printer_module._discover_windows_printers()

        self.assertEqual(
            [printer.get_name() for printer in printers], ["Office", "Zebra"]
        )
        self.assertEqual(printers[0]._backend, "windows")

    @mock.patch.object(printer_module.shutil, "which", return_value="/usr/bin/lpstat")
    @mock.patch.object(printer_module, "_run_command")
    def test_discovers_cups_printers(self, run_command, _which):
        run_command.side_effect = [
            completed([], stdout="Office\nLabel\n"),
            completed(
                [],
                stdout=(
                    "device for Office: ipp://print.example/office\n"
                    "device for Label: usb://vendor/device\n"
                ),
            ),
        ]

        printers = printer_module._discover_cups_printers()

        self.assertEqual(
            [printer.get_name() for printer in printers], ["Label", "Office"]
        )
        self.assertEqual(printers[0]._backend, "cups")
        self.assertNotEqual(printers[0].get_uuid(), printers[1].get_uuid())


class PrinterInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.pdf_path = self.directory / "document.pdf"
        self.pdf_path.write_bytes(b"%PDF-placeholder")
        self.printer = printer_module._make_printer("Office", "office", "cups")

    def tearDown(self):
        self.temporary_directory.cleanup()

    @mock.patch.object(printer_module, "_print_document")
    def test_submits_pdf_with_default_settings(self, print_document):
        self.printer.print(str(self.pdf_path), DefaultSettings())
        print_document.assert_called_once_with(
            "cups", "Office", self.pdf_path.resolve(), ColorMode.COLOR
        )

    @mock.patch.object(printer_module, "_print_document")
    def test_submits_pdf_with_grayscale_settings(self, print_document):
        self.printer.print(str(self.pdf_path), GrayscaleSettings())
        print_document.assert_called_once_with(
            "cups", "Office", self.pdf_path.resolve(), ColorMode.GREY_SCALE
        )

    @mock.patch.object(printer_module, "_print_document")
    def test_submits_image_with_default_settings(self, print_document):
        image_path = self.directory / "photo.png"
        Image.new("RGB", (16, 9), "red").save(image_path)

        self.printer.print(str(image_path))

        print_document.assert_called_once_with(
            "cups", "Office", image_path.resolve(), ColorMode.COLOR
        )

    def test_prints_built_in_test_page_with_default_settings(self):
        captured_path = None

        def capture(path, settings):
            nonlocal captured_path
            captured_path = Path(path)
            self.assertTrue(captured_path.is_file())
            self.assertIsInstance(settings, DefaultSettings)

        with mock.patch.object(self.printer, "print", side_effect=capture):
            self.printer.print_test_page()

        self.assertEqual(captured_path.name, "printer_test_page.pdf")

    def test_prints_built_in_test_page_with_explicit_settings(self):
        settings = GrayscaleSettings()
        with mock.patch.object(self.printer, "print") as print_document:
            self.printer.print_test_page(settings)

        self.assertIs(print_document.call_args.args[1], settings)

    @mock.patch.object(printer_module, "_get_printer_busy", return_value=True)
    def test_get_busy_uses_the_configured_backend(self, get_printer_busy):
        self.assertTrue(self.printer.get_busy())
        get_printer_busy.assert_called_once_with("cups", "Office")

    def test_rejects_missing_input(self):
        with self.assertRaises(FileNotFoundError):
            self.printer.print(str(self.directory / "missing.pdf"), PrintSettings())

    def test_unconfigured_printer_has_no_identity(self):
        printer = PrinterInterface()
        with self.assertRaises(RuntimeError):
            printer.get_name()
        with self.assertRaises(RuntimeError):
            printer.get_uuid()
        with self.assertRaises(RuntimeError):
            printer.get_busy()


class PdfNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _create_source_pdf(self, path: Path) -> None:
        output = canvas.Canvas(str(path), pagesize=landscape(A4))
        output.setFillColorRGB(1, 0, 0)
        output.rect(0, 0, landscape(A4)[0], landscape(A4)[1], stroke=0, fill=1)
        output.showPage()

        output.setPageSize((100 * mm, 100 * mm))
        output.setFillColorRGB(0, 0, 1)
        output.rect(0, 0, 100 * mm, 100 * mm, stroke=0, fill=1)
        output.showPage()
        output.save()

    def _render_page(self, document, page_number):
        page = document[page_number]
        try:
            width, _height = page.get_size()
            bitmap = page.render(scale=210 / width)
            try:
                return bitmap.to_pil().convert("RGB").copy()
            finally:
                bitmap.close()
        finally:
            page.close()

    def test_normalizes_landscape_and_square_pages_to_portrait_a4(self):
        source = self.directory / "source.pdf"
        output = self.directory / "normalized.pdf"
        self._create_source_pdf(source)

        printer_module._write_normalized_pdf(source, output, ColorMode.COLOR)

        document = pdfium.PdfDocument(str(output))
        self.assertEqual(len(document), 2)
        for page_number in range(len(document)):
            page = document[page_number]
            try:
                width, height = page.get_size()
                self.assertAlmostEqual(width, A4[0], delta=1)
                self.assertAlmostEqual(height, A4[1], delta=1)
                self.assertLess(width, height)
            finally:
                page.close()

        landscape_page = self._render_page(document, 0)
        square_page = self._render_page(document, 1)
        try:
            landscape_edge = landscape_page.getpixel((105, 5))
            self.assertGreater(landscape_edge[0], 200)
            self.assertLess(landscape_edge[1], 40)
            self.assertLess(landscape_edge[2], 40)
            top = square_page.getpixel((105, 5))
            center = square_page.getpixel((105, 148))
            self.assertTrue(all(channel > 245 for channel in top))
            self.assertGreater(center[2], 200)
            self.assertLess(center[0], 40)
        finally:
            landscape_page.close()
            square_page.close()
            document.close()

    def test_grayscale_rendering_removes_color(self):
        source = self.directory / "source.pdf"
        output = self.directory / "grayscale.pdf"
        self._create_source_pdf(source)

        printer_module._write_normalized_pdf(source, output, ColorMode.GREY_SCALE)

        document = pdfium.PdfDocument(str(output))
        image = self._render_page(document, 0)
        try:
            red, green, blue = image.getpixel((105, 148))
            self.assertAlmostEqual(red, green, delta=2)
            self.assertAlmostEqual(green, blue, delta=2)
            self.assertLess(red, 200)
        finally:
            image.close()
            document.close()

    def test_rejects_a_malformed_pdf(self):
        source = self.directory / "broken.pdf"
        source.write_text("not a PDF", encoding="utf-8")
        output = self.directory / "unused.pdf"
        with self.assertRaisesRegex(ValueError, "Could not load PDF"):
            printer_module._write_normalized_pdf(source, output, ColorMode.COLOR)


class ImageNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_image_is_contained_and_centered_on_portrait_a4(self):
        source = self.directory / "landscape.png"
        output = self.directory / "normalized.pdf"
        Image.new("RGB", (1600, 900), (230, 20, 30)).save(source)

        printer_module._write_normalized_pdf(source, output, ColorMode.COLOR)

        document = pdfium.PdfDocument(str(output))
        self.assertEqual(len(document), 1)
        page = document[0]
        try:
            width, height = page.get_size()
            self.assertAlmostEqual(width, A4[0], delta=1)
            self.assertAlmostEqual(height, A4[1], delta=1)
            bitmap = page.render(scale=1)
            try:
                image = bitmap.to_pil().convert("RGB")
                for point in (
                    (width // 2, 5),
                    (width // 2, height // 2),
                    (width // 2, height - 6),
                ):
                    red, green, blue = image.getpixel(tuple(map(round, point)))
                    self.assertGreater(red, 180)
                    self.assertLess(green, 60)
                    self.assertLess(blue, 70)
                for point in (
                    (3, 3),
                    (3, height // 2),
                    (width - 4, height // 2),
                    (width - 4, height - 4),
                ):
                    red, green, blue = image.getpixel(tuple(map(round, point)))
                    self.assertGreater(red, 200)
                    self.assertGreater(green, 200)
                    self.assertGreater(blue, 200)
            finally:
                bitmap.close()
        finally:
            page.close()
            document.close()

    def test_image_can_be_rendered_in_grayscale(self):
        source = self.directory / "color.png"
        Image.new("RGB", (100, 200), (20, 140, 240)).save(source)

        pages = printer_module._render_a4_pages(source, ColorMode.GREY_SCALE)
        image = next(pages)
        try:
            self.assertEqual(image.mode, "L")
            self.assertEqual(image.size, printer_module._A4_PIXELS)
        finally:
            image.close()
            pages.close()

    def test_rejects_a_non_pdf_non_image_file(self):
        source = self.directory / "document.txt"
        output = self.directory / "unused.pdf"
        source.write_text("not an image", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "neither a PDF nor a supported image"):
            printer_module._write_normalized_pdf(source, output, ColorMode.COLOR)


class BuiltInTestPageTests(unittest.TestCase):
    def test_test_page_is_single_portrait_a4_with_bilingual_text(self):
        path = Path(printer_module.__file__).parent / "assets" / "printer_test_page.pdf"
        self.assertTrue(path.is_file())

        document = pdfium.PdfDocument(str(path))
        self.assertEqual(len(document), 1)
        page = document[0]
        try:
            width, height = page.get_size()
            self.assertAlmostEqual(width, A4[0], delta=1)
            self.assertAlmostEqual(height, A4[1], delta=1)
            text_page = page.get_textpage()
            try:
                text = text_page.get_text_range()
            finally:
                text_page.close()
            self.assertIn("A4 Printer Test Page", text)
            self.assertIn("打印机测试页", text)
        finally:
            page.close()
            document.close()


class BackendTests(unittest.TestCase):
    @mock.patch.object(printer_module.shutil, "which", return_value="/usr/bin/lpstat")
    @mock.patch.object(printer_module, "_run_command")
    def test_cups_reports_only_an_actively_printing_queue_as_busy(
        self, run_command, _which
    ):
        statuses = (
            ("printer Office now printing Office-42. enabled since today\n", True),
            ("printer Office is idle. enabled since today\n", False),
        )
        for output, expected in statuses:
            with self.subTest(output=output):
                run_command.return_value = completed([], stdout=output)
                self.assertIs(printer_module._cups_printer_busy("Office"), expected)

    @mock.patch.object(printer_module.shutil, "which", return_value="/usr/bin/lpstat")
    @mock.patch.object(printer_module, "_run_command")
    def test_cups_status_failure_is_reported(self, run_command, _which):
        run_command.return_value = completed([], returncode=1, stderr="unknown printer")
        with self.assertRaisesRegex(RuntimeError, "unknown printer"):
            printer_module._cups_printer_busy("Missing")

    def test_windows_reports_a_printing_job_as_busy(self):
        win32print = SimpleNamespace(
            PRINTER_STATUS_PRINTING=1024,
            JOB_STATUS_PRINTING=16,
            OpenPrinter=mock.Mock(return_value="handle"),
            GetPrinter=mock.Mock(return_value={"Status": 0, "cJobs": 2}),
            EnumJobs=mock.Mock(return_value=[{"Status": 0}, {"Status": 16}]),
            ClosePrinter=mock.Mock(),
        )
        with mock.patch.dict(sys.modules, {"win32print": win32print}):
            self.assertTrue(printer_module._windows_printer_busy("Office"))

        win32print.EnumJobs.assert_called_once_with("handle", 0, 2, 1)
        win32print.ClosePrinter.assert_called_once_with("handle")

    def test_windows_reports_an_idle_queue_as_not_busy(self):
        win32print = SimpleNamespace(
            PRINTER_STATUS_PRINTING=1024,
            JOB_STATUS_PRINTING=16,
            OpenPrinter=mock.Mock(return_value="handle"),
            GetPrinter=mock.Mock(return_value={"Status": 0, "cJobs": 1}),
            EnumJobs=mock.Mock(return_value=[{"Status": 1}]),
            ClosePrinter=mock.Mock(),
        )
        with mock.patch.dict(sys.modules, {"win32print": win32print}):
            self.assertFalse(printer_module._windows_printer_busy("Office"))

        win32print.ClosePrinter.assert_called_once_with("handle")

    @mock.patch.object(printer_module, "_print_with_windows")
    def test_windows_converts_an_image_to_a_temporary_a4_pdf(self, print_windows):
        captured_path = None

        def inspect_pdf(_name, path, color_mode):
            nonlocal captured_path
            captured_path = path
            self.assertEqual(path.suffix, ".pdf")
            self.assertIs(color_mode, ColorMode.COLOR)
            document = pdfium.PdfDocument(str(path))
            try:
                self.assertEqual(len(document), 1)
                page = document[0]
                try:
                    width, height = page.get_size()
                    self.assertAlmostEqual(width, A4[0], delta=1)
                    self.assertAlmostEqual(height, A4[1], delta=1)
                finally:
                    page.close()
            finally:
                document.close()

        print_windows.side_effect = inspect_pdf
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "photo.png"
            Image.new("RGB", (160, 90), "cyan").save(source)
            printer_module._print_document("windows", "Office", source, ColorMode.COLOR)

        self.assertIsNotNone(captured_path)
        self.assertFalse(captured_path.exists())

    @mock.patch.object(printer_module.shutil, "which", return_value="/usr/bin/lp")
    @mock.patch.object(printer_module, "_run_command")
    def test_cups_job_requests_a4_portrait_and_grayscale(self, run_command, _which):
        run_command.return_value = completed([], stdout="request id is Office-1")
        printer_module._print_with_cups(
            "Office", Path("normalized.pdf"), ColorMode.GREY_SCALE
        )

        command = run_command.call_args.args[0]
        self.assertIn("media=A4", command)
        self.assertIn("orientation-requested=3", command)
        self.assertIn("print-color-mode=monochrome", command)

    def test_windows_devmode_is_per_job_a4_portrait_and_color(self):
        devmode = SimpleNamespace(Fields=0, Orientation=0, PaperSize=0, Color=0)
        win32con = SimpleNamespace(
            DM_ORIENTATION=1,
            DM_PAPERSIZE=2,
            DM_COLOR=4,
            DMORIENT_PORTRAIT=10,
            DMPAPER_A4=20,
            DMCOLOR_COLOR=30,
            DMCOLOR_MONOCHROME=40,
        )
        win32print = SimpleNamespace(
            OpenPrinter=mock.Mock(return_value="handle"),
            GetPrinter=mock.Mock(return_value={"pDevMode": devmode}),
            ClosePrinter=mock.Mock(),
        )
        with mock.patch.dict(
            sys.modules, {"win32con": win32con, "win32print": win32print}
        ):
            actual = printer_module._windows_devmode("Office", ColorMode.COLOR)

        self.assertIs(actual, devmode)
        self.assertEqual(devmode.Fields, 7)
        self.assertEqual(devmode.Orientation, 10)
        self.assertEqual(devmode.PaperSize, 20)
        self.assertEqual(devmode.Color, 30)
        win32print.ClosePrinter.assert_called_once_with("handle")

    def test_device_fit_preserves_a4_aspect_ratio(self):
        rect = printer_module._fit_device_rect((2480, 3508), (4800, 6500))
        self.assertEqual(rect[0], 102)
        self.assertEqual(rect[1], 0)
        self.assertEqual(rect[2], 4697)
        self.assertEqual(rect[3], 6500)


if __name__ == "__main__":
    unittest.main()
