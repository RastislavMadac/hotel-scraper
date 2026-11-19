"""Unit tests for ExcelExporter single-sheet raw export wrapper."""

from datetime import date
from pathlib import Path
import shutil
import tempfile
import unittest

import openpyxl

from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob
from exporters.excel_generator import ExcelExporter, generate_excel_report


class TestExcelExporter(unittest.TestCase):
    """Test suite for ExcelExporter single-sheet 'Surové dáta' output."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.exports_dir = Path(self.test_dir) / "exports"
        self.exporter = ExcelExporter(exports_dir=self.exports_dir)
        self.job = ScrapingJob(
            check_in=date(2026, 12, 1),
            check_out=date(2026, 12, 5),
            adults=2,
            children=0,
        )
        self.sample_results = [
            ScrapedResult(
                hotel_id="h1",
                hotel_name="Grand Hotel",
                check_in=date(2026, 12, 1),
                check_out=date(2026, 12, 5),
                booking_price=300.0,
                direct_price=270.0,
                offers=[
                    RoomOffer(
                        room_name="Double Standard",
                        price=300.0,
                        capacity_group="2_pax",
                        meal_plan="Raňajky",
                    )
                ],
                direct_offers=[
                    RoomOffer(
                        room_name="Izba Štandard",
                        price=270.0,
                        capacity_group="2_pax",
                        matched_room_name="Double Standard",
                        bonuses="Polpenzia",
                    )
                ],
                status=ResultStatus.SUCCESS,
            ),
        ]

    def tearDown(self) -> None:
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_generate_excel_report_structure_and_values(self) -> None:
        """Verify created Excel file has single 'Surové dáta' sheet with 16 columns and shared matched_room_key."""
        output_file = self.exports_dir / "test_report.xlsx"
        generated_path = self.exporter.generate_report(
            results=self.sample_results,
            output_path=output_file,
            job=self.job,
        )

        self.assertTrue(generated_path.exists())
        self.assertEqual(generated_path, output_file.resolve())

        wb = openpyxl.load_workbook(output_file)
        self.assertEqual(wb.sheetnames, ["Surové dáta"])
        ws = wb["Surové dáta"]
        self.assertIsNone(ws.auto_filter.ref)
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertEqual(ws.max_column, 16)
        self.assertEqual(ws.max_row, 3)

        # Booking row then Direct row (sorted by channel ASC)
        self.assertEqual(ws.cell(row=2, column=3).value, "Grand Hotel")
        self.assertEqual(ws.cell(row=2, column=11).value, "Booking")
        self.assertEqual(ws.cell(row=2, column=13).value, "Double Standard")
        self.assertEqual(ws.cell(row=2, column=14).value, "Double Standard")
        self.assertEqual(ws.cell(row=2, column=15).value, 300.0)

        self.assertEqual(ws.cell(row=3, column=11).value, "Direct")
        self.assertEqual(ws.cell(row=3, column=13).value, "Double Standard")
        self.assertEqual(ws.cell(row=3, column=14).value, "Izba Štandard")
        self.assertEqual(ws.cell(row=3, column=15).value, 270.0)
        wb.close()

    def test_generate_empty_report(self) -> None:
        """Verify generating report with 0 results executes without error."""
        output_file = self.exports_dir / "empty_report.xlsx"
        generated_path = generate_excel_report([], output_path=output_file)

        self.assertTrue(Path(generated_path).exists())
        wb = openpyxl.load_workbook(output_file)
        self.assertEqual(wb.sheetnames, ["Surové dáta"])
        ws = wb["Surové dáta"]
        self.assertEqual(ws.max_row, 1)
        self.assertEqual(ws.max_column, 16)
        wb.close()


if __name__ == "__main__":
    unittest.main()
