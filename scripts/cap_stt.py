"""Capture the L2 Pro serial log and summarise the STT self test.

usage: python cap_stt.py [seconds] [outfile]
Start it right after flashing (esptool resets the chip) or press the reset button.
Bring-up runs 20 s after boot, so 90 s is usually enough.
"""
import serial, time, sys, re

PORT = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].upper().startswith("COM") else "COM83"
args = [a for a in sys.argv[1:] if not a.upper().startswith("COM")]
LIMIT = float(args[0]) if args else 90.0
OUT = args[1] if len(args) > 1 else "stt_run.log"


def open_port(retries=25, delay=1.0):
    for i in range(retries):
        try:
            s = serial.Serial(PORT, 115200, timeout=0.5)
            # keep DTR/RTS low so the ESP32-S3 USB-serial-JTAG does not drop into download mode
            s.dtr = False
            s.rts = False
            return s
        except Exception as e:
            if i == 0:
                print("waiting for %s (%s)" % (PORT, e), flush=True)
            time.sleep(delay)
    return None


s = open_port()
if not s:
    print("FATAL: cannot open", PORT)
    sys.exit(1)
print("opened %s - capturing %.0f s -> %s" % (PORT, LIMIT, OUT), flush=True)
print("hold the on-screen MIC button (right edge) once 'STT: ready' appears\n", flush=True)

fh = open(OUT, "w", encoding="utf-8", errors="replace")
stt = []
t0 = time.time()
pending = ""
while time.time() - t0 < LIMIT:
    d = s.read(4096)
    if not d:
        continue
    txt = d.decode("utf-8", "replace")
    fh.write(txt)
    fh.flush()
    pending += txt
    while "\n" in pending:
        line, pending = pending.split("\n", 1)
        line = line.rstrip("\r")
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if "STT" in line:
            stt.append(line)
s.close()
fh.close()

print("\n" + "=" * 68)
print("STT lines (%d), captured %.1f s" % (len(stt), time.time() - t0))
print("=" * 68)
for line in stt:
    print(line)

joined = "\n".join(stt)
checks = [
    ("model partition found", r"'model' partition at 0x[0-9a-fA-F]+"),
    ("engine loaded", r"engine ready, blob (\d+) bytes"),
    ("clip transcript", r'clip transcript: "(.*)"'),
    ("clip RTF", r"\(RTF ([0-9.]+)\)"),
    ("ES7243E configured", r"ES7243E configured at 0x([0-9a-fA-F]+)"),
    ("I2S RX up", r"I2S0 RX up"),
    ("mic probe peak", r"mic probe read \d+ samples, peak (\d+)"),
    ("ready for talk", r"STT: ready"),
    ("live transcript", r'STT heard: "(.*)"'),
]
print("-" * 68)
for label, pattern in checks:
    m = re.search(pattern, joined)
    print("%-24s %s" % (label, ("OK  " + m.group(0)) if m else "--  not seen"))
if "Guru Meditation" in joined or "abort()" in joined or "Backtrace:" in joined:
    print("\n!! crash detected in the log, see", OUT)