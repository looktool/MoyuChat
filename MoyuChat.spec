# -*- mode: python ; coding: utf-8 -*-
# 打包桌面客户端为单文件 exe（免安装，局域网内直接发给别人用）。
# 精简：排除客户端用不到的标准库/第三方模块，减小体积。
# 构建：pyinstaller MoyuChat.spec --noconfirm --clean   产出 dist/MoyuChat.exe
# 注意：client.py 只用标准库（tkinter/socket/threading/queue/ctypes/json），
#       不依赖 PyMySQL/websockets，故不会把服务器端依赖打进来。

block_cipher = None

excludes = [
    'tkinter.test', 'idlelib', 'turtledemo',
    'unittest', 'test', 'tests', 'doctest', 'pydoc',
    'email', 'http', 'xml', 'xmlrpc',
    'lib2to3', 'distutils', 'setuptools', 'pip',
    'asyncio', 'multiprocessing', 'sqlite3', 'csv',
    'numpy', 'pandas', 'PyMySQL',
]

a = Analysis(
    ['client.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    name='MoyuChat',
    icon='assets/icon.ico',   # 应用图标（python make_icon.py 生成）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # 未安装 UPX；安装后可改 True 进一步压缩
    runtime_tmpdir=None,
    console=False,       # 窗口程序，不弹控制台
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
