"""Excel report generator using Pandas and OpenPyXL for normalized hotel rate exports."""

from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import pandas as pd

from config.settings import get_settings
from core.logger import logger
from core.models import ScrapedResult, ScrapingJob

EXPORTS_DIR = get_settings().exports_dir


class ExportError(Exception):
    """Raised when an error occurs during Excel report generation or file output."""
    pass


class ExcelReportGenerator:
    """Generates clean single-sheet Excel reports from normalized hs-price_comparisons rows."""

    FONT_FAMILY = "Segoe UI"

    HEADER_FILL = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
    HEADER_FONT = Font(name=FONT_FAMILY, color="FFFFFF", bold=True, size=11)
    DATA_FONT = Font(name=FONT_FAMILY, size=10)

    BORDER_THIN = Border(
        left=Side(style="thin", color="D3D3D3"),
        right=Side(style="thin", color="D3D3D3"),
        top=Side(style="thin", color="D3D3D3"),
        bottom=Side(style="thin", color="D3D3D3"),
    )

    CURRENCY_FORMAT = "#,##0.00 €"
    SHEET_RAW_DATA = "Surové dáta"

    RAW_COLUMNS: List[str] = [
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

    def __init__(self, exports_dir: Optional[Union[str, Path]] = None) -> None:
        self.exports_dir = Path(exports_dir or EXPORTS_DIR)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sort_raw_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort rows by scraped_date DESC, hotel_name ASC, check_in ASC, capacity_group ASC, matched_room_key ASC, channel ASC, price ASC."""
        valid_rows = [r for r in (rows or []) if isinstance(r, dict)]

        def _asc_key(r: Dict[str, Any]) -> tuple:
            raw_price = r.get("price")
            try:
                price_num = float(raw_price) if raw_price is not None else float("inf")
            except (TypeError, ValueError):
                price_num = float("inf")
            return (
                str(r.get("hotel_name") or r.get("hotel") or "").lower(),
                str(r.get("check_in") or ""),
                str(r.get("capacity_group") or "").lower(),
                str(r.get("matched_room_key") or "").lower(),
                str(r.get("channel") or "").lower(),
                raw_price is None,
                price_num,
            )

        sorted_asc = sorted(valid_rows, key=_asc_key)
        return sorted(
            sorted_asc,
            key=lambda r: str(r.get("scraped_date") or ""),
            reverse=True,
        )

    @staticmethod
    def _results_to_normalized_rows(
        results: List[ScrapedResult],
        job: Optional[ScrapingJob] = None,
    ) -> List[Dict[str, Any]]:
        """Convert in-memory ScrapedResult objects into normalized per-channel row dicts."""
        rows: List[Dict[str, Any]] = []
        adults = int(getattr(job, "adults", 2)) if job is not None else 2
        children = int(getattr(job, "children", 0)) if job is not None else 0
        children_ages_list = getattr(job, "children_ages", None) if job is not None else []
        children_ages_str = ", ".join(str(a) for a in (children_ages_list or []))

        for res in results or []:
            scraped_dt = (
                res.scraped_at
                if isinstance(res.scraped_at, datetime)
                else datetime.utcnow()
            )
            scraped_date_str = scraped_dt.date().isoformat()
            scraped_at_str = scraped_dt.isoformat(timespec="seconds")

            cin_date = res.check_in or (getattr(job, "check_in", None) if job else None)
            cout_date = res.check_out or (getattr(job, "check_out", None) if job else None)
            check_in_str = (
                cin_date.isoformat()
                if isinstance(cin_date, date)
                else str(cin_date or "")
            )
            check_out_str = (
                cout_date.isoformat()
                if isinstance(cout_date, date)
                else str(cout_date or "")
            )
            nights = (
                max((cout_date - cin_date).days, 1)
                if (isinstance(cin_date, date) and isinstance(cout_date, date))
                else (getattr(job, "nights", 1) if job else 1)
            )
            lead_time = (
                (cin_date - scraped_dt.date()).days
                if isinstance(cin_date, date)
                else 0
            )

            direct_offers = [
                d_off
                for d_off in list(getattr(res, "direct_offers", None) or [])
                if getattr(d_off, "room_name", None)
                and str(d_off.room_name).strip() not in ("", "—")
                and d_off.price is not None
                and d_off.price > 0
            ]
            booking_offers = [
                b_off
                for b_off in list(getattr(res, "offers", None) or [])
                if getattr(b_off, "room_name", None)
                and str(b_off.room_name).strip() not in ("", "—")
                and b_off.price is not None
                and b_off.price > 0
            ]

            for d_off in direct_offers:
                d_name = str(d_off.room_name).strip()
                raw_matched = getattr(d_off, "matched_room_name", None)
                matched_key = (
                    str(raw_matched).strip()
                    if raw_matched and str(raw_matched).strip() not in ("", "—")
                    else f"Direct Only: {d_name}"
                )
                d_bonuses = (
                    d_off.bonuses
                    if d_off.bonuses is not None
                    else (
                        " | ".join(
                            p for p in (d_off.meal_plan, d_off.cancellation_policy) if p
                        )
                        or None
                    )
                )
                rows.append(
                    {
                        "scraped_date": scraped_date_str,
                        "scraped_at": scraped_at_str,
                        "hotel_id": res.hotel_id,
                        "hotel_name": res.hotel_name,
                        "check_in": check_in_str,
                        "check_out": check_out_str,
                        "nights": nights,
                        "lead_time": lead_time,
                        "adults": adults,
                        "children": children,
                        "children_ages": children_ages_str,
                        "channel": "Direct",
                        "capacity_group": d_off.capacity_group or None,
                        "matched_room_key": matched_key,
                        "room_name": d_name,
                        "price": round(float(d_off.price), 2),
                        "bonuses": d_bonuses,
                    }
                )

            for b_off in booking_offers:
                b_name = str(b_off.room_name).strip()
                b_bonuses = (
                    b_off.bonuses
                    if b_off.bonuses is not None
                    else (
                        " | ".join(
                            p for p in (b_off.meal_plan, b_off.cancellation_policy) if p
                        )
                        or None
                    )
                )
                rows.append(
                    {
                        "scraped_date": scraped_date_str,
                        "scraped_at": scraped_at_str,
                        "hotel_id": res.hotel_id,
                        "hotel_name": res.hotel_name,
                        "check_in": check_in_str,
                        "check_out": check_out_str,
                        "nights": nights,
                        "lead_time": lead_time,
                        "adults": adults,
                        "children": children,
                        "children_ages": children_ages_str,
                        "channel": "Booking",
                        "capacity_group": b_off.capacity_group or None,
                        "matched_room_key": b_name,
                        "room_name": b_name,
                        "price": round(float(b_off.price), 2),
                        "bonuses": b_bonuses,
                    }
                )

        return rows

    def generate_report(
        self,
        results: List[ScrapedResult],
        output_path: Optional[Union[str, Path]] = None,
        job: Optional[ScrapingJob] = None,
    ) -> Path:
        """Generate single-sheet raw Excel report from ScrapedResult objects."""
        if output_path is None:
            output_path = (
                self.exports_dir
                / f"hotel_report_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            )
        rows = self._results_to_normalized_rows(results, job=job)
        return self.generate_raw_db_report(rows=rows, output_path=output_path)

    def generate_comparison_report(
        self,
        results: List[ScrapedResult],
        output_path: Union[str, Path],
        include_details: bool = True,
        job: Optional[ScrapingJob] = None,
    ) -> str:
        """Generate single-sheet raw Excel report and return its string path."""
        rows = self._results_to_normalized_rows(results, job=job)
        generated = self.generate_raw_db_report(rows=rows, output_path=output_path)
        return str(generated)

    def generate_raw_db_report(
        self,
        rows: List[Dict[str, Any]],
        output_path: Optional[Union[str, Path]] = None,
    ) -> Path:
        """Export normalized hs-price_comparisons database rows to a single clean Excel sheet."""
        try:
            target_path = (
                Path(output_path).resolve()
                if output_path is not None
                else (
                    self.exports_dir
                    / f"hotel_raw_db_report_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                ).resolve()
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)

            sorted_rows = self._sort_raw_rows(rows)
            mapped_rows: List[Dict[str, Any]] = []

            for r in sorted_rows:
                raw_ages = r.get("children_ages")
                if isinstance(raw_ages, (list, tuple)):
                    ages_val = ", ".join(str(a) for a in raw_ages)
                else:
                    ages_val = raw_ages

                hotel_val = (
                    r.get("hotel_name")
                    if r.get("hotel_name") is not None
                    else r.get("hotel")
                )

                mapped_rows.append(
                    {
                        "Dátum zberu": r.get("scraped_date"),
                        "Čas merania": r.get("scraped_at"),
                        "Hotel": hotel_val,
                        "Check-in": r.get("check_in"),
                        "Check-out": r.get("check_out"),
                        "Počet nocí": r.get("nights"),
                        "Lead time (dni)": r.get("lead_time"),
                        "Dospelí": r.get("adults"),
                        "Deti": r.get("children"),
                        "Vek detí": ages_val,
                        "Kanál": r.get("channel"),
                        "Kapacitná skupina": r.get("capacity_group"),
                        "Párovací kľúč (Room Group)": r.get("matched_room_key"),
                        "Názov izby": r.get("room_name"),
                        "Cena (€)": r.get("price"),
                        "Bonusy a podmienky": r.get("bonuses"),
                    }
                )

            df_raw = pd.DataFrame(mapped_rows, columns=self.RAW_COLUMNS)

            with pd.ExcelWriter(str(target_path), engine="openpyxl") as writer:
                df_raw.to_excel(writer, sheet_name=self.SHEET_RAW_DATA, index=False)
                ws = writer.sheets[self.SHEET_RAW_DATA]
                ws.views.sheetView[0].showGridLines = True
                ws.freeze_panes = "A2"

                ws.row_dimensions[1].height = 24
                for col_idx in range(1, len(self.RAW_COLUMNS) + 1):
                    cell = ws.cell(row=1, column=col_idx)
                    cell.fill = self.HEADER_FILL
                    cell.font = self.HEADER_FONT
                    cell.alignment = Alignment(
                        horizontal="center", vertical="center", wrap_text=True
                    )
                    cell.border = self.BORDER_THIN

                for row_idx in range(2, len(df_raw) + 2):
                    for col_idx in range(1, len(self.RAW_COLUMNS) + 1):
                        cell = ws.cell(row=row_idx, column=col_idx)
                        cell.font = self.DATA_FONT
                        cell.border = self.BORDER_THIN

                        if col_idx in (1, 2, 4, 5, 6, 7, 8, 9, 11, 12):
                            cell.alignment = Alignment(
                                horizontal="center", vertical="center"
                            )
                        elif col_idx == 15:
                            cell.alignment = Alignment(
                                horizontal="right", vertical="center"
                            )
                            if cell.value is not None and not pd.isna(cell.value):
                                cell.number_format = self.CURRENCY_FORMAT
                        elif col_idx == 16:
                            cell.alignment = Alignment(
                                vertical="center", wrap_text=True
                            )
                        else:
                            cell.alignment = Alignment(vertical="center")

                for col in ws.columns:
                    col_letter = get_column_letter(col[0].column)
                    max_len = max(len(str(cell.value or "")) for cell in col)
                    ws.column_dimensions[col_letter].width = min(max(max_len + 4, 12), 45)

            logger.info(
                f"Raw DB Excel report successfully generated ({len(mapped_rows)} rows) at: {target_path}"
            )
            return target_path

        except Exception as exc:
            logger.error(f"Zlyhanie pri generovaní surového DB Excel reportu: {exc}")
            raise ExportError(
                f"Zlyhanie pri generovaní surového DB Excel reportu: {exc}"
            ) from exc


ExcelExporter = ExcelReportGenerator


def generate_excel_report(
    results: List[ScrapedResult],
    output_path: Union[str, Path],
    job: Optional[ScrapingJob] = None,
) -> str:
    """Convenience wrapper creating a clean single-sheet raw Excel report."""
    generator = ExcelReportGenerator()
    return generator.generate_comparison_report(results, output_path=output_path, job=job)
