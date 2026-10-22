"""生成 MoyuChat 应用图标 assets/icon.ico（多分辨率），供 PyInstaller 使用。

运行：python make_icon.py   （依赖 Pillow）
"""

import os

from PIL import Image, ImageDraw, ImageFont

SIZES = [16, 24, 32, 48, 64, 128, 256]
BLUE = (0, 82, 217, 255)  # #0052D9 主色


def find_font(size):
    for p in [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
              r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\segoeui.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def make(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1],
                        radius=int(size * 0.22), fill=BLUE)
    f = find_font(int(size * 0.6))
    text = "M"
    bbox = d.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
           text, font=f, fill=(255, 255, 255, 255))
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)
    imgs = [make(s) for s in SIZES]
    ico = os.path.join(out_dir, "icon.ico")
    imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
    imgs[-1].save(os.path.join(out_dir, "icon.png"))
    print("生成图标:", ico)


if __name__ == "__main__":
    main()
