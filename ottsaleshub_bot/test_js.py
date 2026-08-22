"""Parse-check every browser script. Run after touching anything in static/."""
import re, subprocess, sys, pathlib
root = pathlib.Path(__file__).parent if __name__ != "__main__" else pathlib.Path(".")
fails = 0
for f in sorted(pathlib.Path("static").glob("*")):
    if f.suffix not in (".html", ".js"):
        continue
    src = f.read_text(encoding="utf-8")
    js = src
    if f.suffix == ".html":
        m = re.search(r'<script type="module">(.*?)</script>', src, re.S)
        if not m:
            continue
        js = m.group(1)
    tmp = pathlib.Path("/tmp") / (f.name + ".mjs")
    tmp.write_text(js)
    r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
    print(f"{f.name:18} {'OK' if r.returncode == 0 else 'FAIL'}")
    if r.returncode:
        print(r.stderr.strip()[:400]); fails += 1
sys.exit(1 if fails else 0)
