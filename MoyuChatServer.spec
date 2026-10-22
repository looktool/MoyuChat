# -*- mode: python ; coding: utf-8 -*-
# 打包「一体化服务器」为单文件 exe：TCP 聊天 + UDP 自动发现 + 网页托管，一键局域网起服。
# 构建：pyinstaller MoyuChatServer.spec --noconfirm --clean   产出 dist/MoyuChatServer.exe
# 存储默认 JSON（零依赖）；设置 MOYU_STORAGE=mysql 可用 MySQL（已内置 PyMySQL）。

block_cipher = None

excludes = [
    'tkinter', '_tkinter', 'tkinter.test', 'idlelib', 'turtledemo',
    'unittest', 'test', 'tests', 'doctest', 'pydoc',
    'lib2to3', 'distutils', 'setuptools', 'pip',
    'numpy', 'pandas', 'PIL', 'PyInstaller',
]

a = Analysis(
    ['server_app.py'],
    pathex=['.'],
    binaries=[],
    # 把构建好的前端一起打包进 exe（source -> 解压后相对路径）
    datas=[('web/dist', 'web/dist')],
    hiddenimports=['pymysql'],   # MOYU_STORAGE=mysql 时需要（默认 JSON 用不到）
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MoyuChatServer',
    icon='assets/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,        # 服务器保留控制台，便于查看局域网地址与日志
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
