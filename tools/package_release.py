"""Package only executable dependencies and public documentation, never local data."""
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

source = Path(__file__).resolve().parents[1]
root = source.parent
app = root/"app"
licenses = root/"licenses"
licenses.mkdir(exist_ok=True)
for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "pyqtgraph", "pyserial", "numpy", "psutil", "pyinstaller", "packaging"):
    dist = metadata.distribution(name)
    for file in dist.files or []:
        if file.name.lower().startswith(("license", "copying", "authors")):
            target = licenses/name/str(file).replace("..", "_")
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(dist.locate_file(file), target)
python_license = Path(sys.base_prefix)/"LICENSE.txt"
if python_license.exists():
    shutil.copy2(python_license, licenses/"Python-LICENSE.txt")
for name,url in (("LGPL-3.0.txt","https://www.gnu.org/licenses/lgpl-3.0.txt"),
                 ("GPL-3.0.txt","https://www.gnu.org/licenses/gpl-3.0.txt")):
    target=licenses/name
    if not target.exists():
        with urllib.request.urlopen(url,timeout=30) as response:
            target.write_bytes(response.read())

archive = root/"ElectrolyteManager-2.0.0-Windows-x64.zip"
commit = subprocess.check_output(["git","rev-parse","HEAD"],cwd=source,text=True).strip()
manifest = {"version":"2.0.0","source_commit":commit,"built_utc":datetime.now(timezone.utc).isoformat(),
            "validation":"Pre-release; 24 h endurance and external gravimetric validation not yet complete",
            "dependencies":{n:metadata.version(n) for n in ("PySide6","pyqtgraph","pyserial","numpy")}}
(root/"release.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for directory in (app,licenses):
        for path in directory.rglob("*"):
            if path.is_file():
                z.write(path, "Electrolyte-Manager-2.0/"+path.relative_to(root).as_posix())
    for path in (source/"README.md",source/"LICENSE",root/"release.json"):
        z.write(path,"Electrolyte-Manager-2.0/"+path.name)
    for path in (source/"docs").glob("*.md"):
        z.write(path,"Electrolyte-Manager-2.0/docs/"+path.name)
    z.writestr("Electrolyte-Manager-2.0/data/.keep","")
    z.writestr("Electrolyte-Manager-2.0/exports/.keep","")
with archive.open("rb") as f:
    digest=hashlib.file_digest(f,"sha256").hexdigest()
archive.with_suffix(".zip.sha256").write_text(digest+"  "+archive.name+"\n",encoding="ascii")
with zipfile.ZipFile(archive) as z:
    bad=[n for n in z.namelist() if n.lower().endswith((".sqlite",".sqlite3",".log",".key",".pem")) or "/data/" in n and not n.endswith(".keep")]
    assert not bad,bad
report={**manifest,"archive":str(archive),"sha256":digest,"size_MiB":archive.stat().st_size/1024**2,"private_content_check":"passed"}
(root/"exports/package-check.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
print(json.dumps(report,indent=2))
