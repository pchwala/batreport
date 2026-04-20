# Battery Report

A minimal battery diagnostic tool for Linux service teams. Reads live battery data via `upower`, shows real-time status labels, plots charge/energy/voltage graphs, records sessions to CSV, and exports reports to PDF.

## Features

- Live status labels (state, charge %, energy Wh, voltage V) updated every second
- Dual-axis graph: Percentage % and Energy Wh vs elapsed time
- Voltage graph vs elapsed time
- Export graphs to PDF

## Prerequisites

- **Python 3.10+**
- **upower** — installed via your distro's package manager, e.g.:

  ```bash
  # Debian / Ubuntu
  sudo apt install upower

  # Arch
  sudo pacman -S upower

  # Fedora
  sudo dnf install upower
  ```

## Quickstart

```bash
git clone https://github.com/yourname/batreport.git
cd batreport
./install.sh
./run.sh
```

`install.sh` creates a `.venv` virtual environment and installs Python dependencies from `requirements.txt`. You only need to run it once (or after pulling updates).

## License

MIT
