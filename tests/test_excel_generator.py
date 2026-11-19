"""Unit tests for ExcelReportGenerator single-sheet normalized raw DB exports."""

from datetime import date
from pathlib import Path
import shutil
import tempfile
import unittest

import openpyxl

from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob
from exporters.excel_generator import ExcelReportGenerator, ExportError

EXPECTED_16_HEADERS = [
    "Dátum zberu",
    "Čas merania",
    "Hotel",
    "Check-in",
    "Check-out",
    "Počet nocí",
    "Lead time (dni)",
    "Dospelí",
    "Deti",
    "Vek detí",
    "Kanál",
    "Kapacitná skupina",
    "Párovací kľúč (Room Group)",
    "Názov izby",
    "Cena (€)",
    "Bonusy a podmienky",
]


class TestExcelReportGenerator(unittest.TestCase):
    """Test suite for ExcelReportGenerator normalized single-sheet 'Surové dáta' exports."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.output_file = Path(self.test_dir) / "test_hotel_report.xlsx"
        self.generator = ExcelReportGenerator(exports_dir=self.test_dir)

    def tearDown(self) -> None:
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_generate_raw_db_report_single_sheet_16_columns_and_sorting(self) -> None:
        """Verify single 'Surové dáta' sheet, 16 headers, sorting, freeze_panes, wrap_text, and no filters."""
        raw_output = Path(self.test_dir) / "raw_db_export.xlsx"
        db_rows = [
            {
                "scraped_date": "2026-11-17",
                "scraped_at": "2026-11-17T22:10:00",
                "hotel_name": "Grand Hotel Permon",
                "check_in": "2026-12-01",
                "check_out": "2026-12-04",
                "nights": 3,
                "lead_time": 14,
                "adults": 2,
                "children": 1,
                "children_ages": "7",
                "channel": "Direct",
                "capacity_group": "2_plus_1",
                "matched_room_key": "Izba Premium s balkónom",
                "room_name": "2-lôžková Premium PLUS",
                "price": 450.0,
                "bonuses": "Polpenzia | Wellness v cene | Dlhší popis balíka pre otestovanie zalamovania textu",
            },
            {
                "scraped_date": "2026-11-18",
                "scraped_at": "2026-11-18T08:00:00",
                "hotel_name": "Grand Hotel Permon",
                "check_in": "2026-12-01",
                "check_out": "2026-12-04",
                "nights": 3,
                "lead_time": 13,
                "adults": 2,
                "children": 1,
                "children_ages": "7",
                "channel": "Direct",
                "capacity_group": "2_plus_1",
                "matched_room_key": "Izba Premium s balkónom",
                "room_name": "2-lôžková Premium PLUS",
                "price": 445.0,
                "bonuses": "Polpenzia | Wellness v cene",
            },
            {
                "scraped_date": "2026-11-18",
                "scraped_at": "2026-11-18T08:00:00",
                "hotel_name": "Grand Hotel Permon",
                "check_in": "2026-12-01",
                "check_out": "2026-12-04",
                "nights": 3,
                "lead_time": 13,
                "adults": 2,
                "children": 1,
                "children_ages": "7",
                "channel": "Booking",
                "capacity_group": "2_plus_1",
                "matched_room_key": "Izba Premium s balkónom",
                "room_name": "Izba Premium s balkónom",
                "price": 490.0,
                "bonuses": "Raňajky v cene | Nevratná",
            },
        ]

        out_path = self.generator.generate_raw_db_report(
            rows=db_rows, output_path=raw_output
        )
        self.assertEqual(out_path, raw_output.resolve())
        self.assertTrue(out_path.exists())

        wb = openpyxl.load_workbook(out_path)
        self.assertEqual(wb.sheetnames, ["Surové dáta"])
        ws = wb["Surové dáta"]

        # No auto_filter, no charts, frozen header row A2
        self.assertIsNone(ws.auto_filter.ref)
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertEqual(len(ws._charts), 0)

        actual_headers = [
            ws.cell(row=1, column=idx).value for idx in range(1, 17)
        ]
        self.assertEqual(actual_headers, EXPECTED_16_HEADERS)

        # Header bold style
        self.assertTrue(ws.cell(row=1, column=1).font.bold)

        # Sorting: 2026-11-18 first (scraped_date DESC), then channel ASC ('Booking' before 'Direct')
        self.assertEqual(ws.cell(row=2, column=1).value, "2026-11-18")
        self.assertEqual(ws.cell(row=2, column=11).value, "Booking")
        self.assertEqual(ws.cell(row=2, column=13).value, "Izba Premium s balkónom")
        self.assertEqual(ws.cell(row=2, column=14).value, "Izba Premium s balkónom")
        self.assertEqual(ws.cell(row=2, column=15).value, 490.0)
        self.assertEqual(ws.cell(row=2, column=15).number_format, "#,##0.00 €")
        self.assertTrue(ws.cell(row=2, column=16).alignment.wrap_text)

        self.assertEqual(ws.cell(row=3, column=1).value, "2026-11-18")
        self.assertEqual(ws.cell(row=3, column=11).value, "Direct")
        self.assertEqual(ws.cell(row=3, column=13).value, "Izba Premium s balkónom")
        self.assertEqual(ws.cell(row=3, column=14).value, "2-lôžková Premium PLUS")
        self.assertEqual(ws.cell(row=3, column=15).value, 445.0)

        self.assertEqual(ws.cell(row=4, column=1).value, "2026-11-17")
        self.assertEqual(ws.cell(row=4, column=15).value, 450.0)

        # Column widths capped at max 45 chars
        for col_dim in ws.column_dimensions.values():
            if col_dim.width is not None:
                self.assertLessEqual(col_dim.width, 45)

        wb.close()

    def test_generate_comparison_report_outputs_single_raw_sheet_per_channel(self) -> None:
        """generate_comparison_report outputs single 'Surové dáta' sheet with separate Direct & Booking rows."""
        job = ScrapingJob(
            check_in=date(2026, 12, 1),
            check_out=date(2026, 12, 4),
            adults=2,
            children=0,
        )
        results = [
            ScrapedResult(
                hotel_id="hotel_kempinski",
                hotel_name="Grand Hotel Kempinski",
                check_in=date(2026, 12, 1),
                check_out=date(2026, 12, 4),
                booking_price=160.0,
                direct_price=155.0,
                offers=[
                    RoomOffer(
                        room_name="Štandardná izba",
                        price=160.0,
                        meal_plan="Raňajky v cene",
                        cancellation_policy="Bezplatné storno",
                        capacity_group="2_pax",
                        matched_room_name="Izba Classic",
                    )
                ],
                direct_offers=[
                    RoomOffer(
                        room_name="Izba Classic",
                        price=155.0,
                        bonuses="Polpenzia | Wellness",
                        capacity_group="2_pax",
                        matched_room_name="Štandardná izba",
                    ),
                    RoomOffer(
                        room_name="Rodinný apartmán 2+2",
                        price=320.0,
                        bonuses="Polpenzia",
                        capacity_group="family",
                        matched_room_name=None,
                    ),
                ],
                status=ResultStatus.SUCCESS,
            )
        ]

        ret_path = self.generator.generate_comparison_report(
            results=results,
            output_path=self.output_file,
            job=job,
        )
        self.assertEqual(ret_path, str(self.output_file.resolve()))
        self.assertTrue(self.output_file.exists())

        wb = openpyxl.load_workbook(self.output_file)
        self.assertEqual(wb.sheetnames, ["Surové dáta"])
        ws = wb["Surové dáta"]
        self.assertEqual(ws.max_row, 4)  # 1 header + 3 offer rows (1 Booking + 2 Direct)

        rows_data = [
            (ws.cell(row=r, column=11).value, ws.cell(row=r, column=13).value, ws.cell(row=r, column=14).value, ws.cell(row=r, column=15).value)
            for r in range(2, 5)
        ]
        self.assertIn(("Booking", "Štandardná izba", "Štandardná izba", 160.0), rows_data)
        self.assertIn(("Direct", "Štandardná izba", "Izba Classic", 155.0), rows_data)
        self.assertIn(("Direct", "Direct Only: Rodinný apartmán 2+2", "Rodinný apartmán 2+2", 320.0), rows_data)
        wb.close()

    def test_export_error_on_invalid_output_path(self) -> None:
        """Verify that file write error raises custom ExportError."""
        invalid_path = "Z:\\non_existent_drive_xyz\\report.xlsx"
        with self.assertRaises(ExportError):
            self.generator.generate_raw_db_report(
                rows=[],
                output_path=invalid_path,
            )


if __name__ == "__main__":
    unittest.main()
