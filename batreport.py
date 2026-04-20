#!/usr/bin/env python3
"""Battery Report Tool – service team battery diagnostic for Linux."""

import csv
import io
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QFont, QPainter, QPageSize, QPdfWriter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Battery helpers
# ---------------------------------------------------------------------------


def get_battery_path() -> str:
    """Return the first BAT device path reported by upower."""
    result = subprocess.run(
        ["upower", "-e"], capture_output=True, text=True, check=True
    )
    for line in result.stdout.splitlines():
        if "BAT" in line:
            return line.strip()
    raise RuntimeError("No battery device found via 'upower -e'")


def parse_battery(text: str) -> dict:
    """Extract relevant fields from 'upower -i' output."""
    data: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        val = rest.strip()
        if key == "percentage":
            data["percentage"] = float(val.rstrip("%"))
        elif key == "energy-full-design":
            data["energy_full_design"] = float(val.split()[0])
        elif key == "energy-full":
            data["energy_full"] = float(val.split()[0])
        elif key == "energy":
            data["energy"] = float(val.split()[0])
        elif key == "voltage":
            data["voltage"] = float(val.split()[0])
        elif key == "state":
            data["state"] = val
    return data


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------


def get_device_info() -> dict:
    """Collect device model and serial number from DMI sysfs (readable without root)."""
    try:
        vendor = Path("/sys/class/dmi/id/sys_vendor").read_text().strip()
        product = Path("/sys/class/dmi/id/product_name").read_text().strip()
        model = f"{vendor} {product}"
    except OSError:
        model = "Unknown"
    try:
        serial = Path("/sys/class/dmi/id/product_serial").read_text().strip()
    except OSError:
        serial = "Unknown"
    return {"model": model, "serial": serial}


# ---------------------------------------------------------------------------
# Custom time axis
# ---------------------------------------------------------------------------


class TimeAxisItem(pg.AxisItem):
    """Formats elapsed seconds as  Xs  /  Xm Ys  /  Xh Ym."""

    def tickStrings(self, values, scale, spacing):
        labels = []
        for v in values:
            v = int(max(v, 0))
            if v < 60:
                labels.append(f"{v}s")
            elif v < 3600:
                m, s = divmod(v, 60)
                labels.append(f"{m}m {s:02d}s")
            else:
                h, rem = divmod(v, 3600)
                labels.append(f"{h}h {rem // 60:02d}m")
        return labels


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, battery_path: str) -> None:
        super().__init__()
        self.battery_path = battery_path
        self.setWindowTitle("Battery Report")

        # Always-on timer for status labels; graphs update only when recording
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self._recording = False
        self._loaded = False
        self._start_time: datetime | None = None
        self._csv_file = None
        self._csv_writer = None

        self._t: list[float] = []
        self._pct: list[float] = []
        self._energy: list[float] = []
        self._energy_fd: list[float] = []
        self._voltage: list[float] = []

        self._device_info = get_device_info()

        self._build_ui()
        self._timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        vbox = QVBoxLayout(central)
        vbox.setSpacing(6)

        # Status labels
        row = QHBoxLayout()
        self._lbl_state = QLabel("State: —")
        self._lbl_pct = QLabel("Charge: —")
        self._lbl_energy = QLabel("Energy: —")
        self._lbl_efd = QLabel("Design cap: —")
        self._lbl_voltage = QLabel("Voltage: —")
        for lbl in (self._lbl_state, self._lbl_pct, self._lbl_energy, self._lbl_efd, self._lbl_voltage):
            row.addWidget(lbl)
        row.addStretch()
        vbox.addLayout(row)

        # Device info row
        self._lbl_device_info = QLabel(
            f"Device: {self._device_info['model']}   S/N: {self._device_info['serial']}"
        )
        vbox.addWidget(self._lbl_device_info)

        # Button row
        btn_row = QHBoxLayout()
        self._btn = QPushButton("Test")
        self._btn.setFixedHeight(36)
        self._btn.clicked.connect(self._toggle)
        self._btn_load = QPushButton("Load CSV")
        self._btn_load.setFixedHeight(36)
        self._btn_load.clicked.connect(self._load_csv)
        self._btn_export = QPushButton("Export PDF")
        self._btn_export.setFixedHeight(36)
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._export_pdf)
        for b in (self._btn, self._btn_load, self._btn_export):
            btn_row.addWidget(b)
        vbox.addLayout(btn_row)

        vbox.addSpacing(70)

        # ---- Graph 1: Percentage (left) + Energy Wh (right) ----
        self._plot1 = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")},
            title="Charge & Energy vs Time",
        )
        self._plot1.setLabel("left", "Percentage", units="%")
        self._plot1.setLabel("bottom", "Elapsed")
        self._plot1.showGrid(x=True, y=True, alpha=0.3)
        self._plot1.showAxis("right")
        self._plot1.setYRange(0, 105)

        # Secondary ViewBox for energy curves
        self._vb2 = pg.ViewBox()
        self._plot1.scene().addItem(self._vb2)
        self._plot1.getAxis("right").linkToView(self._vb2)
        self._plot1.getAxis("right").setLabel("Energy", units="Wh")
        self._vb2.setXLink(self._plot1.getViewBox())
        self._vb2.enableAutoRange(axis="y")
        self._plot1.getViewBox().sigResized.connect(self._sync_views)

        legend1 = self._plot1.addLegend(offset=(10, 10))
        self._curve_pct = self._plot1.plot(pen=pg.mkPen("g", width=2), name="Percentage %")
        self._curve_energy = pg.PlotCurveItem(pen=pg.mkPen("c", width=2))
        self._vb2.addItem(self._curve_energy)
        legend1.addItem(self._curve_energy, "Energy Wh")

        vbox.addWidget(self._plot1, stretch=1)

        # ---- Graph 2: Voltage ----
        self._plot2 = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")},
            title="Voltage vs Time",
        )
        self._plot2.setLabel("left", "Voltage", units="V")
        self._plot2.setLabel("bottom", "Elapsed")
        self._plot2.showGrid(x=True, y=True, alpha=0.3)
        self._plot2.addLegend(offset=(10, 10))
        self._curve_voltage = self._plot2.plot(pen=pg.mkPen("y", width=2), name="Voltage V")

        vbox.addWidget(self._plot2, stretch=1)

        self.resize(900, 700)

    def _sync_views(self) -> None:
        """Keep secondary ViewBox geometry in sync with the main ViewBox."""
        self._vb2.setGeometry(self._plot1.getViewBox().sceneBoundingRect())
        self._vb2.linkedViewChanged(self._plot1.getViewBox(), self._vb2.XAxis)

    # ------------------------------------------------------------------
    # Test / Stop
    # ------------------------------------------------------------------

    def _toggle(self) -> None:
        if self._recording:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        self._loaded = False
        self._t.clear()
        self._pct.clear()
        self._energy.clear()
        self._energy_fd.clear()
        self._voltage.clear()

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        csv_path = SCRIPT_DIR / f"batreport_{ts}.csv"
        self._csv_file = open(csv_path, "w", newline="")
        self._csv_file.write(f"# model: {self._device_info['model']}\n")
        self._csv_file.write(f"# serial: {self._device_info['serial']}\n")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow([
            "datetime", "elapsed_s", "percentage",
            "energy_wh", "energy_full_wh", "energy_full_design_wh",
            "voltage_v", "state",
        ])

        self._start_time = datetime.now()
        self._recording = True
        self._btn.setText("Stop")
        self._update_buttons()

    def _stop(self) -> None:
        self._recording = False
        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
        self._btn.setText("Test")
        self._update_buttons()

    # ------------------------------------------------------------------
    # Button state helpers + CSV load + PDF export
    # ------------------------------------------------------------------

    def _update_buttons(self) -> None:
        has_data = len(self._t) > 0
        self._btn.setEnabled(not self._loaded)
        self._btn_export.setEnabled(has_data and not self._recording)
        self._btn_load.setEnabled(not self._recording)

    def _load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load CSV", str(SCRIPT_DIR), "CSV Files (*.csv)"
        )
        if not path:
            return

        self._t.clear()
        self._pct.clear()
        self._energy.clear()
        self._energy_fd.clear()
        self._voltage.clear()

        try:
            with open(path, newline="") as f:
                raw_lines = f.readlines()

            # Parse # metadata comment lines
            meta: dict = {}
            data_lines = []
            for line in raw_lines:
                if line.startswith("# "):
                    key, _, val = line[2:].partition(": ")
                    meta[key.strip()] = val.strip()
                else:
                    data_lines.append(line)

            if meta:
                self._device_info["model"] = meta.get("model", "Unknown")
                self._device_info["serial"] = meta.get("serial", "Unknown")
                self._lbl_device_info.setText(
                    f"Device: {self._device_info['model']}   S/N: {self._device_info['serial']}"
                )

            reader = csv.DictReader(io.StringIO("".join(data_lines)))
            last_row = None
            for row in reader:
                self._t.append(float(row["elapsed_s"]))
                self._pct.append(float(row["percentage"]))
                self._energy.append(float(row["energy_wh"]))
                self._energy_fd.append(float(row["energy_full_design_wh"]))
                self._voltage.append(float(row["voltage_v"]))
                last_row = row
        except Exception as exc:
            print(f"CSV load error: {exc}", file=sys.stderr)
            return

        self._curve_pct.setData(self._t, self._pct)
        self._curve_energy.setData(self._t, self._energy)
        self._curve_voltage.setData(self._t, self._voltage)

        if last_row:
            self._lbl_state.setText(f"State: {last_row.get('state', '—')}")
            self._lbl_pct.setText(f"Charge: {float(last_row['percentage']):.1f}%")
            self._lbl_energy.setText(f"Energy: {float(last_row['energy_wh']):.3f} Wh")
            self._lbl_efd.setText(f"Design cap: {float(last_row['energy_full_design_wh']):.3f} Wh")
            self._lbl_voltage.setText(f"Voltage: {float(last_row['voltage_v']):.3f} V")

        self._loaded = True
        self._update_buttons()

    def _export_pdf(self) -> None:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        default_name = str(SCRIPT_DIR / f"batreport_{ts}.pdf")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export PDF", default_name, "PDF Files (*.pdf)"
        )
        if not path:
            return

        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setResolution(150)

        painter = QPainter(writer)
        page_rect = writer.pageLayout().paintRectPixels(writer.resolution())
        pw = page_rect.width()
        ph = page_rect.height()

        pad = ph // 10  # 10% of page height as vertical padding

        # Logo
        svg = QSvgRenderer(str(SCRIPT_DIR / "logo.svg"))
        logo_natural = svg.defaultSize()
        logo_h = 80
        logo_w = logo_natural.width() * logo_h // logo_natural.height() if logo_natural.height() > 0 else logo_h
        logo_x = (pw - logo_w) // 2
        svg.render(painter, QRect(logo_x, 0, logo_w, logo_h))

        # Header text
        font = QFont("Arial", 14)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(0, logo_h + pad - 30, f"Battery Report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        font2 = QFont("Arial", 10)
        painter.setFont(font2)
        painter.drawText(0, logo_h + pad, f"Device: {self._device_info['model']}   S/N: {self._device_info['serial']}")

        header_h = logo_h + pad + 30  # bottom of info text + small margin

        img1 = self._plot1.grab().toImage()
        img2 = self._plot2.grab().toImage()

        # Available height for both graphs with one pad gap between them and pad at bottom
        available_h = ph - header_h - pad - pad

        # Phase 1: scale each image to full page width, preserving aspect ratio
        w1, h1 = pw, img1.height() * pw // img1.width()
        w2, h2 = pw, img2.height() * pw // img2.width()

        # Phase 2: if combined height exceeds available space, scale both down uniformly
        total_h = h1 + h2
        if total_h > available_h:
            ratio = available_h / total_h
            w1 = int(w1 * ratio)
            h1 = int(h1 * ratio)
            w2 = int(w2 * ratio)
            h2 = int(h2 * ratio)

        # Centre horizontally if narrower than page width (after phase 2 scale-down)
        x1 = (pw - w1) // 2
        x2 = (pw - w2) // 2

        painter.drawImage(QRect(x1, header_h, w1, h1), img1)
        painter.drawImage(QRect(x2, header_h + h1 + pad, w2, h2), img2)

        painter.end()

    # ------------------------------------------------------------------
    # Timer tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            result = subprocess.run(
                ["upower", "-i", self.battery_path],
                capture_output=True, text=True, check=True,
            )
            data = parse_battery(result.stdout)
        except Exception as exc:
            print(f"upower error: {exc}", file=sys.stderr)
            return

        pct = data.get("percentage", 0.0)
        energy = data.get("energy", 0.0)
        efd = data.get("energy_full_design", 0.0)
        efull = data.get("energy_full", 0.0)
        voltage = data.get("voltage", 0.0)
        state = data.get("state", "—")

        # Always update status labels
        self._lbl_state.setText(f"State: {state}")
        self._lbl_pct.setText(f"Charge: {pct:.1f}%")
        self._lbl_energy.setText(f"Energy: {energy:.3f} Wh")
        self._lbl_efd.setText(f"Design cap: {efd:.3f} Wh")
        self._lbl_voltage.setText(f"Voltage: {voltage:.3f} V")

        if not self._recording:
            return

        now = datetime.now()
        elapsed = (now - self._start_time).total_seconds()

        self._t.append(elapsed)
        self._pct.append(pct)
        self._energy.append(energy)
        self._energy_fd.append(efd)
        self._voltage.append(voltage)

        self._csv_writer.writerow([
            now.isoformat(timespec="seconds"),
            f"{elapsed:.1f}",
            pct, energy, efull, efd, voltage, state,
        ])
        self._csv_file.flush()

        self._curve_pct.setData(self._t, self._pct)
        self._curve_energy.setData(self._t, self._energy)
        self._curve_voltage.setData(self._t, self._voltage)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._recording:
            self._stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = QApplication(sys.argv)
    try:
        bat_path = get_battery_path()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    window = MainWindow(bat_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
