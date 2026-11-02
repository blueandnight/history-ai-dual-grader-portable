# PyInstaller onedir specification. The data directory is deliberately kept
# outside the executable bundle so it can move with the portable drive.
from pathlib import Path
from importlib.util import find_spec
from PyInstaller.utils.hooks import collect_all, copy_metadata, collect_data_files

pdfium_data, pdfium_binaries, pdfium_hidden = collect_all("pypdfium2")
raw_data, raw_binaries, raw_hidden = collect_all("pypdfium2_raw")

# Retain model data for the existing objective handwriting recognizer only.
recognizer_data=collect_data_files('rapidocr_onnxruntime',includes=['**/*.onnx','**/*.yaml','**/*.txt']) if find_spec('rapidocr_onnxruntime') else []
project_dir = Path(SPECPATH)

a = Analysis(
    [str(project_dir / "app.py")],
    pathex=[str(project_dir)],
    binaries=pdfium_binaries + raw_binaries,
    datas=[
        (str(project_dir / "static"), "static"),
        (str(project_dir / "objective_static"), "objective_static"),
    ] + recognizer_data + pdfium_data + raw_data + copy_metadata("pypdfium2"),
    hiddenimports=pdfium_hidden + raw_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="历史学科AI双评阅卷系统",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    a.zipfiles,
    a.zipped_data,
    strip=False,
    upx=False,
    name="历史学科AI双评阅卷系统",
)
