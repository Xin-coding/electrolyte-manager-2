# 2.1.1 — Metrohm serial handshake compatibility

The previous connection always required PC CTS. On the locally tested COM9 Prolific adapter/cable, the Metrohm receive-ready signal instead appeared on DSR (CTS low, DSR high). This prevented transmission and caused repeated `Write timeout` reconnects even though the instrument was correctly set to 9600, 8N1, HWs.

- Keep RTS/CTS when CTS is asserted; when CTS is low and DSR is high, select DTR/DSR hardware flow control before the first query. Do not silently disable all flow control.
- Show the active serial parameters on the connection page, and include line states in send-timeout diagnostics.
- Close the port when the identity/mode handshake fails.
- Probe Metrohm first during automatic detection. After a successful identity reply, do not send the pump or conductivity protocols to that port. Other baud rates can cause RS error 38 on a Metrohm.
- Keep the instrument at 9600 baud, 8 data bits, 1 stop bit, parity none, handshake HWs, pH mode.

Read-only bench evidence is stored in `../exports/rs232-diagnosis-20261029/` relative to the source directory. Pump commands and measurement/calibration settings were not changed. This local repair is not a public release.
