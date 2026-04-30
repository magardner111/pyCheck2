#!/usr/bin/env python3
"""
ycheck_printer.py - Native macOS check printer (replaces ycheck2.exe + Wine)

Parses the VB6 printer-command CSV inside .ycheck2 files and renders
directly to any macOS printer via QPrinter/QPainter.
"""

import sys
import os
import zipfile
import shutil
import csv
import re
import json
import datetime

os.environ.setdefault("MVK_CONFIG_LOG_LEVEL", "0")

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QMessageBox, QSpinBox,
    QCheckBox, QGroupBox, QFormLayout, QComboBox,
    QTreeWidget, QTreeWidgetItem, QAbstractItemView, QHeaderView,
    QDialog, QLineEdit, QCompleter,
)
from PySide6.QtCore import Qt, QRectF
from PySide6.QtPrintSupport import QPrinter, QPrintDialog, QPrinterInfo
from PySide6.QtGui import QPainter, QFont, QPageSize, QPageLayout, QFontDatabase

# GnuMICR font bundled alongside this script (GPL-2, Eric Sandeen)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MICR_FONT_PATH = os.path.join(_SCRIPT_DIR, "fonts", "GnuMICR.ttf")
MICR_FONT_FAMILY = "GnuMICR"

PASSWORD = b'*6-/&c-qHUp =p*!*4U@8xF=(|:!+f'

# VB6 ScaleMode 1 = Twip (1440/inch), ScaleMode 2 = Point (72/inch)
TWIPS_TO_PT = 72.0 / 1440.0

# Logical page size in points (8.5 × 11 inches)
PAGE_W = 612.0
PAGE_H = 792.0

SETTINGS_PATH = os.path.expanduser("~/.ycheck_printer_settings.json")
HISTORY_CSV = os.path.expanduser("~/.ycheck_print_history.csv")
HISTORY_FIELDS = ["Printed At", "Date", "Check #", "Payable To", "Total"]


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def load_history():
    rows = []
    if not os.path.exists(HISTORY_CSV):
        return rows
    try:
        with open(HISTORY_CSV, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception:
        pass
    return rows


def append_history(metadata_list):
    write_header = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if write_header:
            writer.writeheader()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for m in metadata_list:
            writer.writerow({
                "Printed At": ts,
                "Date": m["date"],
                "Check #": m["check_num"],
                "Payable To": m["payable_to"],
                "Total": m["total"],
            })


def history_duplicates(metadata_list):
    """Return subset of metadata_list whose check numbers are already in history."""
    existing = {row["Check #"] for row in load_history()}
    return [m for m in metadata_list if m["check_num"] in existing]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_csv_pages(csv_path):
    """
    Parse a VB6 printer-command CSV into a list of pages.
    Each page is a list of draw-op dicts.
    One EndDoc command = end of one page.
    """
    pages = []
    page = []

    font_name = "Arial"
    font_size = 8.0
    font_bold = False
    cur_x = cur_y = 0.0
    scale_mode = 2  # 2=Point, 1=Twip

    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = next(csv.reader([raw]))
            except Exception:
                continue
            if not row:
                continue

            cmd = row[0]
            val = row[1] if len(row) > 1 else ""

            if cmd == "FontName":
                font_name = val
            elif cmd == "FontSize":
                try:
                    font_size = float(val)
                except ValueError:
                    pass
            elif cmd == "FontBold":
                font_bold = val == "-1"
            elif cmd == "CurrentX":
                try:
                    cur_x = float(val)
                except ValueError:
                    pass
            elif cmd == "CurrentY":
                try:
                    cur_y = float(val)
                except ValueError:
                    pass
            elif cmd == "ScaleMode":
                try:
                    scale_mode = int(val)
                except ValueError:
                    pass
            elif cmd in ("Print", "Print2", "NonNegotiable"):
                factor = 1.0 if scale_mode == 2 else TWIPS_TO_PT
                page.append(
                    {
                        "cmd": cmd,
                        "text": val,
                        "x": cur_x * factor,
                        "y": cur_y * factor,
                        "font_name": font_name,
                        "font_size": font_size,
                        "font_bold": font_bold,
                    }
                )
            elif cmd == "EndDoc":
                if page:
                    pages.append(page)
                page = []

    if page:
        pages.append(page)

    return pages


def _check_number(page):
    """Return the check number found in a page's draw ops, or None."""
    for op in page:
        m = re.search(r"CK#:(\d+)", op["text"])
        if m:
            return m.group(1)
    return None


def _check_metadata(page):
    """Extract display metadata from a page's draw ops."""
    date = check_num = total = payable_to = ""
    for op in page:
        text = op["text"]
        if "CK#:" in text and "DATE:" in text:
            m = re.search(r"DATE:([\d/]+)", text)
            if m:
                date = m.group(1)
            m = re.search(r"CK#:(\d+)", text)
            if m:
                check_num = m.group(1)
            m = re.search(r"TOTAL:\$?([\d,]+\.\d{2})", text)
            if m:
                total = m.group(1).replace(",", "")
        elif text.startswith("PAYEE:") and not payable_to:
            raw = text[len("PAYEE:"):].strip()
            payable_to = re.sub(r"\([^)]*\)\s*$", "", raw).strip()
    return {"date": date, "check_num": check_num, "payable_to": payable_to, "total": total}


def extract_unique_pages(ycheck2_path):
    """
    Unzip the .ycheck2 file, parse all CSV printer commands, and return
    one page per unique check number (first occurrence wins).
    """
    zip_path = ycheck2_path + ".tmp.zip"
    temp_dir = ycheck2_path + ".tmp_dir"

    try:
        shutil.copy(ycheck2_path, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.setpassword(PASSWORD)
            zf.extractall(temp_dir)

        all_pages = []
        for root, _, files in os.walk(temp_dir):
            for fname in sorted(files):
                if fname.endswith(".csv") or fname.endswith(".txt"):
                    all_pages.extend(_parse_csv_pages(os.path.join(root, fname)))

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(zip_path):
            os.remove(zip_path)

    # Deduplicate: keep first page per check number.
    # The file contains one EndDoc per check; duplicate check numbers arise
    # because the same check is sometimes repeated in the CSV data.
    seen = set()
    unique = []
    for page in all_pages:
        ck = _check_number(page)
        if ck is None or ck not in seen:
            if ck:
                seen.add(ck)
            unique.append(page)

    return unique


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_pages(pages, printer, copies_only=False):
    """
    Draw all pages onto QPrinter using QPainter.

    All VB6 coordinates are in points (1/72 inch) after parsing.  We convert
    to device pixels manually using the printer's actual DPI, then set font
    sizes via setPixelSize() so Qt's point-size logic never interferes.

    The AdvMICR font (magnetic ink) is substituted with Courier New since it is
    a speciality font unlikely to be installed on macOS.
    """
    dpi = printer.resolution()
    pt_to_px = dpi / 72.0  # 1 VB6 point -> this many device pixels

    painter = QPainter()
    if not painter.begin(printer):
        return False

    first = True
    for page in pages:
        if not first:
            printer.newPage()
        first = False

        for op in page:
            if op["cmd"] == "NonNegotiable" and not copies_only:
                continue
            fname = (
                MICR_FONT_FAMILY
                if op["font_name"].lower() == "advmicr"
                else op["font_name"]
            )
            font = QFont(fname)
            font.setBold(op["font_bold"])
            # Use pixel size so the result is DPI-correct regardless of how
            # Qt's point→pixel conversion would otherwise interact with the
            # printer's logical DPI.
            font.setPixelSize(max(1, int(round(op["font_size"] * pt_to_px))))
            painter.setFont(font)

            # Convert VB6 point coordinates to device pixels.
            px = op["x"] * pt_to_px
            py = op["y"] * pt_to_px
            pw = (PAGE_W - op["x"]) * pt_to_px
            ph = op["font_size"] * pt_to_px * 3.0

            painter.drawText(
                QRectF(px, py, pw, ph),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                op["text"],
            )

    painter.end()
    return True


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class PrinterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pyCheck2 Printer")
        self.pages = []
        self._settings = load_settings()
        self._printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        self._printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        self._printer.setPageOrientation(QPageLayout.Orientation.Portrait)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # --- File row ---
        file_row = QHBoxLayout()
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color: #888;")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(browse_btn)
        root.addLayout(file_row)

        self.info_label = QLabel("")
        root.addWidget(self.info_label)

        # --- Check list ---
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Date", "Check #", "Payable To", "Total"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.tree)

        sel_row = QHBoxLayout()
        sel_all_btn = QPushButton("Select All")
        sel_all_btn.clicked.connect(self._select_all)
        sel_none_btn = QPushButton("Select None")
        sel_none_btn.clicked.connect(self._select_none)
        sel_row.addWidget(sel_all_btn)
        sel_row.addWidget(sel_none_btn)
        sel_row.addStretch()
        self.total_label = QLabel("Total: $0.00")
        font = self.total_label.font()
        font.setBold(True)
        self.total_label.setFont(font)
        sel_row.addWidget(self.total_label)
        root.addLayout(sel_row)

        # --- Print settings box ---
        box = QGroupBox("Print Settings")
        form = QFormLayout(box)

        # Printer selector
        self.printer_combo = QComboBox()
        self._available_printers = [p.printerName() for p in QPrinterInfo.availablePrinters()]
        self.printer_combo.addItems(self._available_printers)
        saved = self._settings.get("default_printer", "")
        if saved in self._available_printers:
            self.printer_combo.setCurrentText(saved)
        elif self._available_printers:
            # Fall back to the system default
            sys_default = QPrinterInfo.defaultPrinterName()
            if sys_default in self._available_printers:
                self.printer_combo.setCurrentText(sys_default)
        self.printer_combo.currentTextChanged.connect(self._on_printer_changed)
        self._apply_selected_printer()

        printer_row = QHBoxLayout()
        printer_row.addWidget(self.printer_combo, 1)
        save_btn = QPushButton("Save as Default")
        save_btn.clicked.connect(self._save_default_printer)
        printer_row.addWidget(save_btn)
        form.addRow("Printer:", printer_row)

        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 99)
        self.copies_spin.setValue(1)
        form.addRow("Number of Copies:", self.copies_spin)

        self.copies_only_cb = QCheckBox()
        form.addRow("Print Copies Only:", self.copies_only_cb)

        self.collate_cb = QCheckBox()
        form.addRow("Collate:", self.collate_cb)

        root.addWidget(box)

        # --- Buttons ---
        btn_row = QHBoxLayout()
        self.print_btn = QPushButton("Print")
        self.print_btn.setEnabled(False)
        self.print_btn.clicked.connect(self._print)
        history_btn = QPushButton("History")
        history_btn.clicked.connect(self._show_history)
        btn_row.addWidget(self.print_btn)
        btn_row.addWidget(history_btn)
        root.addLayout(btn_row)

    def _apply_selected_printer(self):
        name = self.printer_combo.currentText()
        if name:
            self._printer.setPrinterName(name)

    def _on_printer_changed(self, name):
        self._apply_selected_printer()

    def _save_default_printer(self):
        name = self.printer_combo.currentText()
        self._settings["default_printer"] = name
        save_settings(self._settings)
        QMessageBox.information(self, "Saved", f'"{name}" saved as your default printer.')

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open yCheck2 File", filter="YCheck2 Files (*.ycheck2)"
        )
        if not path:
            return

        try:
            pages = extract_unique_pages(path)
        except (zipfile.BadZipFile, RuntimeError) as e:
            QMessageBox.critical(self, "Error", f"Could not open file:\n{e}")
            return
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.pages = pages
        self.file_label.setText(os.path.basename(path))
        self.file_label.setStyleSheet("")
        self._populate_tree()
        self._update_print_btn()

    def _populate_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        for page in self.pages:
            m = _check_metadata(page)
            item = QTreeWidgetItem([
                m["date"], m["check_num"], m["payable_to"],
                f"${float(m['total']):,.2f}" if m["total"] else "",
            ])
            item.setCheckState(0, Qt.CheckState.Checked)
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)
        count = self.tree.topLevelItemCount()
        self.info_label.setText(f"{count} check(s) loaded")
        self._update_print_btn()

    def _on_item_changed(self, item, col):
        self._update_print_btn()

    def _all_items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _select_all(self):
        self.tree.blockSignals(True)
        for item in self._all_items():
            if item:
                item.setCheckState(0, Qt.CheckState.Checked)
        self.tree.blockSignals(False)
        self._update_print_btn()

    def _select_none(self):
        self.tree.blockSignals(True)
        for item in self._all_items():
            if item:
                item.setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_print_btn()

    def _selected_pages(self):
        selected = []
        for i, item in enumerate(self._all_items()):
            if item and item.checkState(0) == Qt.CheckState.Checked:
                selected.append(self.pages[i])
        return selected

    def _update_print_btn(self):
        selected = self._selected_pages()
        self.print_btn.setEnabled(bool(selected))
        total = 0.0
        for i, item in enumerate(self._all_items()):
            if item and item.checkState(0) == Qt.CheckState.Checked:
                m = _check_metadata(self.pages[i])
                try:
                    total += float(m["total"])
                except (ValueError, KeyError):
                    pass
        self.total_label.setText(f"Total: ${total:,.2f}")

    def _print(self):
        if not self.pages:
            return

        pages = self._selected_pages()
        metadata = [_check_metadata(p) for p in pages]

        # Duplicate check
        dupes = history_duplicates(metadata)
        if dupes:
            lines = "\n".join(
                f"  Check #{d['check_num']}  –  {d['payable_to'] or 'Unknown payee'}"
                for d in dupes
            )
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setWindowTitle("Already Printed")
            msg.setText(
                "The following check(s) may have already been printed:\n\n"
                f"{lines}\n\n"
                "Do you want to continue?"
            )
            msg.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
            cancel = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            if msg.clickedButton() is cancel:
                return

        self._apply_selected_printer()
        self._printer.setCopyCount(self.copies_spin.value())
        self._printer.setCollateCopies(self.collate_cb.isChecked())

        dlg = QPrintDialog(self._printer, self)
        if dlg.exec() != QPrintDialog.DialogCode.Accepted:
            return

        if not render_pages(pages, self._printer, copies_only=self.copies_only_cb.isChecked()):
            QMessageBox.critical(self, "Print Error", "Failed to start the print job.")
        else:
            append_history(metadata)
            QMessageBox.information(
                self,
                "Print Job Sent",
                f'Sent {len(pages)} check(s) to "{self._printer.printerName()}".',
            )

    def _show_history(self):
        rows = load_history()
        dlg = QDialog(self)
        dlg.setWindowTitle("Check Print History")
        dlg.resize(820, 480)
        layout = QVBoxLayout(dlg)

        # --- Search bar ---
        search_row = QHBoxLayout()
        search_box = QLineEdit()
        search_box.setPlaceholderText("Search by printed date, check date, check #, payable to, or total…")

        # Build autocomplete from all cell values across all rows/columns
        all_values = sorted({
            cell
            for row in rows
            for cell in [
                row.get("Printed At", ""),
                row.get("Date", ""),
                row.get("Check #", ""),
                row.get("Payable To", ""),
                row.get("Total", ""),
            ]
            if cell
        })
        completer = QCompleter(all_values, dlg)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        search_box.setCompleter(completer)

        clear_btn = QPushButton("Clear Search")
        clear_btn.clicked.connect(lambda: (search_box.clear(), search_box.setFocus()))

        search_row.addWidget(search_box)
        search_row.addWidget(clear_btn)
        layout.addLayout(search_row)

        # Esc clears and refocuses the search box instead of closing the dialog
        def handle_key(event):
            if event.key() == Qt.Key.Key_Escape:
                search_box.clear()
                search_box.setFocus()
            else:
                QDialog.keyPressEvent(dlg, event)
        dlg.keyPressEvent = handle_key

        # --- History tree ---
        tree = QTreeWidget()
        tree.setColumnCount(len(HISTORY_FIELDS))
        tree.setHeaderLabels(HISTORY_FIELDS)
        tree.setRootIsDecorated(False)
        tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        def fmt_total(raw):
            try:
                return f"${float(raw):,.2f}"
            except (ValueError, TypeError):
                return raw

        def populate(filter_text=""):
            tree.clear()
            needle = filter_text.strip().lower()
            for row in reversed(rows):
                cells = [
                    row.get("Printed At", ""),
                    row.get("Date", ""),
                    row.get("Check #", ""),
                    row.get("Payable To", ""),
                    fmt_total(row.get("Total", "")),
                ]
                if needle and not any(needle in c.lower() for c in cells):
                    continue
                QTreeWidgetItem(tree, cells)

        populate()
        search_box.textChanged.connect(populate)

        layout.addWidget(tree)

        if not rows:
            layout.addWidget(QLabel("No print history yet."))

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Load bundled GnuMICR font so MICR lines render with proper symbols
    if os.path.exists(MICR_FONT_PATH):
        QFontDatabase.addApplicationFont(MICR_FONT_PATH)

    win = PrinterWindow()
    win.resize(700, 560)
    screen = app.primaryScreen().geometry()
    win.move(
        (screen.width() - win.width()) // 2,
        (screen.height() - win.height()) // 2,
    )
    win.show()
    sys.exit(app.exec())
