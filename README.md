# eMonitor Dashboard

A live, read-only web dashboard for the SQLite database maintained by the
[`emonitor`](https://github.com/paulbeerkens/emonitor) collector. It is designed
to run as a separate application on the same Raspberry Pi 5.

## What it shows

- current combined Main Power and Solar Panels readings;
- the five circuits using the most power right now;
- a selectable, interactive 24-hour graph built from 15-second readings;
- automatic refresh every 15 seconds;
- click-to-select behavior on every top-five circuit card.
- Raspberry Pi CPU temperature, free disk space, and fan speed when exposed by
  the Pi's Linux kernel.
- the latest ComEd five-minute market price, estimated current cost rate, and
  top 20 circuits over the past 24 hours in both kWh and variable cost.
- a half-hour dollar-per-hour bar chart whose current slot is recalculated from
  the collector's in-progress energy accumulator every 15 seconds.
- today's and yesterday's missing data in minutes and as a coverage percentage.
- a clickable SiteSage-style donut showing the 24-hour energy share of every
  named branch circuit;
- Raspberry Pi CPU utilization and memory use alongside temperature, disk, and
  fan health.

Paired 240-volt channels do not need special dashboard logic. The collector
combines reciprocal channel pairs before it writes `power_samples`, and this
dashboard reads those already-combined logical circuits.

## Install on the Pi

Keep the two repositories beside one another:

```text
/home/pbeerkens/src/
├── emonitor/
└── emonitor-dashboard/
```

Then:

```bash
cd ~/src/emonitor-dashboard
env/install.sh
vim .env
.venv/bin/python app.py
```

Open `http://emonitor.local:5000/` from another computer on the home network.
The default `.env` already points to `../emonitor/db/emonitor.db`.

For continuous operation:

```bash
cd ~/src/emonitor-dashboard
sudo deployment/install-service.sh
```

Useful service commands:

```bash
sudo systemctl status emonitor-dashboard
sudo journalctl -u emonitor-dashboard -f
sudo systemctl restart emonitor-dashboard
```

## Development

Install the development dependencies and run the tests:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The dashboard opens SQLite in read-only/query-only mode and uses short-lived
connections. The collector remains the only writer, and SQLite WAL mode allows
the dashboard to read while collection continues.

Pi health values are read from Linux `sysfs` and the root filesystem. No extra
Pi packages or privileged service access are required. Fan speed displays as
unavailable if the installed fan or its driver does not expose an RPM sensor.

The dashboard refreshes ComEd's public five-minute feed at most once every five
minutes. Cost estimates add the bill-derived variable rate configured by
`COMED_VARIABLE_ADDON_CENTS_PER_KWH`; fixed charges are intentionally excluded.

