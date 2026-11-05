"""Build the Windows portable application from a clean, identified commit."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from em2 import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=SOURCE).strip():
        raise SystemExit("Build requires a clean source checkout")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    parts = tuple(map(int, __version__.split(".")))+(0,)
    resource = output/"version-info.txt"
    resource.write_text(f'''VSVersionInfo(ffi=FixedFileInfo(filevers={parts!r}, prodvers={parts!r}, mask=0x3f, flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0,0)), kids=[StringFileInfo([StringTable('040904B0', [StringStruct('FileDescription','Electrolyte Manager'), StringStruct('FileVersion','{__version__}'), StringStruct('ProductName','Electrolyte Manager 3.1'), StringStruct('ProductVersion','{__version__}')])]), VarFileInfo([VarStruct('Translation',[1033,1200])])])''', encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
               "--name", "ElectrolyteManager", "--version-file", str(resource),
               "--paths", str(SOURCE), "--add-data", str(SOURCE/"em2/resources")+";em2/resources",
               "--distpath", str(output), "--workpath", str(output/"cache"), "--specpath", str(output)]
    for package in ("scipy", "matplotlib", "pandas", "pytest", "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"):
        command += ["--exclude-module", package]
    command.append(str(SOURCE/"tools/launcher.py"))
    # Keep unrelated scientific tools / agent utilities out of DLL discovery.
    # In particular an old app-local UCRT can break Qt before Python starts.
    environment = os.environ.copy()
    windows = Path(environment.get("WINDIR", "C:/Windows"))
    environment["PATH"] = os.pathsep.join(map(str, (Path(sys.executable).parent, Path(sys.base_prefix),
                                                   Path(sys.base_prefix)/"DLLs", windows/"System32", windows)))
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    with (output/"build.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=SOURCE, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    app = output/"ElectrolyteManager"
    hashes = {}
    for file in sorted(app.rglob("*")):
        if file.is_file():
            with file.open("rb") as stream:
                hashes[file.relative_to(app).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {"version": __version__, "source_commit": commit, "built_utc": datetime.now(timezone.utc).isoformat(),
              "app": str(app), "files": hashes}
    (output/"build-manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, indent=2))


if __name__ == "__main__":
    main()
