"""生成 desktop/assets/icon.ico。

放一份生成脚本而不是一张来路不明的图:图标要能重生成,改了主题色也跟得上。

用法(在仓库任意位置):
    python desktop/assets/make-icon.py

设计:深石板蓝的圆角方块 + 一个琥珀色的圆点。
    底色和界面同源(#161A20),琥珀是界面里「它停下来等你」的那个颜色 ——
    一个点,在一片暗里亮着,就是这东西在值班的样子。
    「dots」这个名字也在里面:那个点就是 dot。
      - 16px 下只看得到一个亮点,够用
      - 大尺寸下外围一圈更暗的环,给它一点结构
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (22, 26, 32, 255)          # #161A20 和 theme.css 的 --bg 一致
DOT = (224, 164, 88, 255)       # #E0A458 --wait
RING = (224, 164, 88, 64)       # 同一个琥珀,压暗当外环

SIZE = 1024                     # 先画大的,再让 Pillow 缩成各个尺寸
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def render(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方块。macOS/Windows 都圆得比较克制,~22% 是常见的手感
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=BG)

    cx = cy = size / 2
    # 外环:只有大尺寸看得见,小尺寸下它会糊进底色,不影响
    r_ring = size * 0.30
    d.ellipse([cx - r_ring, cy - r_ring, cx + r_ring, cy + r_ring],
              outline=RING, width=max(1, int(size * 0.035)))
    # 那个点
    r_dot = size * 0.155
    d.ellipse([cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot], fill=DOT)

    return img


def main() -> None:
    out = Path(__file__).with_name("icon.ico")
    render().save(out, format="ICO", sizes=SIZES)
    print(f"写好了 {out}({', '.join(f'{w}x{h}' for w, h in SIZES)})")


if __name__ == "__main__":
    main()
