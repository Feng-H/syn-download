#!/usr/bin/env python3
"""生成 PyInstaller 的 Windows 版本信息文件。用法:python make_version.py 0.2.1"""
import sys

ver = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"
parts = [int(x) for x in ver.split(".")] + [0, 0, 0]
fv = ", ".join(str(x) for x in parts[:4])

content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({fv}),
    prodvers=({fv}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[StringFileInfo([
    StringTable('080404b0', [
      StringStruct('CompanyName', 'Feng-H'),
      StringStruct('FileDescription', 'SynDL - Synology DSM Downloader'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('ProductName', 'SynDL'),
      StringStruct('ProductVersion', '{ver}'),
      StringStruct('OriginalFilename', 'SynDL.exe'),
    ])
  ])
])
"""
with open("version.txt", "w", encoding="utf-8") as f:
    f.write(content)
print(f"version.txt written for {ver}")
