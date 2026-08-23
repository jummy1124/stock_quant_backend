"""`.xlsx` export for a backtest run — summary sheet + per-stock detail sheet.

Self-contained (openpyxl plus the response schema) so the backtest feature can
be lifted out with its router and engine, exactly as the download module can.

Returns raw bytes for the router to stream with a Content-Disposition header.
"""
from __future__ import annotations

import io
from typing import Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.schemas import BacktestResponse

_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
_MODE_LABEL = {
    "intraday_to_close": "盤中13:00 → 收盤價",
    "close_to_close": "收盤價 → 收盤價",
}


def _autosize(ws, widths: Sequence[int]) -> None:
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w


def _style_header(ws, row_idx: int, ncols: int) -> None:
    for col in range(1, ncols + 1):
        c = ws.cell(row=row_idx, column=col)
        c.font = Font(bold=True)
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center")


_SUMMARY_HEADERS = [
    "N (交易日)", "樣本數", "上漲", "下跌", "持平", "無資料",
    "上漲機率", "平均報酬%", "中位數報酬%", "最佳%", "最差%",
]
_SUMMARY_WIDTHS = [11, 9, 8, 8, 8, 9, 11, 12, 14, 10, 10]

_DETAIL_HEADERS = [
    "篩選日", "代號", "名稱", "市場", "進場價", "出場日", "出場價", "漲跌", "報酬%",
]
_SORT_LABEL = {
    "trade_date": "篩選日", "symbol": "代號", "entry_price": "進場價",
    "exit_date": "出場日", "exit_price": "出場價", "change": "漲跌",
    "return_pct": "報酬%",
}
_DETAIL_WIDTHS = [12, 9, 13, 7, 10, 12, 10, 9, 10]


def backtest_to_xlsx(result: BacktestResponse) -> bytes:
    """Two sheets: the win-rate table per N, then every settled trade at
    `result.detail_n`.

    The detail sheet carries only the horizon the caller asked to detail, not
    every horizon — the same stock appears once per N, and a workbook repeating
    each row six times is harder to read, not more informative.
    """
    wb = Workbook()

    ws = wb.active
    ws.title = "統計"
    mode_label = _MODE_LABEL.get(result.mode, result.mode)
    ws.append([
        f"起漲個股回測（{mode_label}）  {result.start:%Y-%m-%d} ~ {result.end:%Y-%m-%d}"
        f"  共 {result.trading_days} 個篩選日、{result.entries} 筆訊號"
    ])
    ws.cell(row=1, column=1).font = Font(bold=True, size=12)
    ws.merge_cells(
        start_row=1, start_column=1, end_row=1, end_column=len(_SUMMARY_HEADERS)
    )

    ws.append(_SUMMARY_HEADERS)
    _style_header(ws, 2, len(_SUMMARY_HEADERS))
    for stat in result.summary:
        ws.append([
            stat.n, stat.samples, stat.wins, stat.losses, stat.flat, stat.missing,
            stat.win_rate, stat.avg_return_pct, stat.median_return_pct,
            stat.best_return_pct, stat.worst_return_pct,
        ])
    for row in ws.iter_rows(min_row=3, min_col=7, max_col=7):
        for cell in row:
            cell.number_format = "0.00%"
    _autosize(ws, _SUMMARY_WIDTHS)

    if result.warning:
        ws.append([])
        ws.append([f"⚠️ {result.warning}"])
    ws.append([])
    ws.append(["⚠️ 統計為歷史資訊參考，非投資建議。"])

    det = wb.create_sheet(f"明細 N={result.detail_n}")
    order_label = "由大到小" if result.detail_order == "desc" else "由小到大"
    sort_label = _SORT_LABEL.get(result.detail_sort, result.detail_sort)
    det.append([
        f"N = {result.detail_n} 個交易日後的個股表現"
        f"（共 {result.detail_total} 筆，本表列出 {len(result.detail)} 筆；"
        f"依「{sort_label}」{order_label}排序）"
    ])
    det.cell(row=1, column=1).font = Font(bold=True, size=12)
    det.merge_cells(
        start_row=1, start_column=1, end_row=1, end_column=len(_DETAIL_HEADERS)
    )
    det.append(_DETAIL_HEADERS)
    _style_header(det, 2, len(_DETAIL_HEADERS))
    for row in result.detail:
        det.append([
            f"{row.trade_date:%Y-%m-%d}", row.symbol, row.name, row.market,
            row.entry_price, f"{row.exit_date:%Y-%m-%d}", row.exit_price,
            row.change, row.return_pct,
        ])
    _autosize(det, _DETAIL_WIDTHS)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def empty_backtest_xlsx(message: str) -> bytes:
    """A one-line workbook explaining why there is nothing to export.

    The endpoint answers 200 with this rather than 404 so the browser still
    downloads something a person can open and understand, matching how the
    snapshot downloads behave for an empty day.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "回測"
    ws.append([message])
    ws.cell(row=1, column=1).font = Font(bold=True, size=12)
    _autosize(ws, [90])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
