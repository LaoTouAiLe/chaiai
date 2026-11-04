"""配对二维码（架构 §24）。

**为什么值得单独做**：把配对链接从电脑搬到手机是整个流程里最烦的一步——复制粘贴、发微信、
手抄都会出错。二维码不需要装任何 App，系统相机或微信扫一下就能打开配对页。

**终端里为什么用块字符**：43 字符的链接编成二维码约 33×33 模块，用 ``##`` 双行表示是 66 行，
超过一屏；TTY 下用半块字符压到 17 行左右。非 TTY（输出被重定向到文件、或在 CI 里）退回
``##``，保证任何环境都能看。
"""

from __future__ import annotations

import io

import qrcode
import qrcode.image.svg
from qrcode.constants import ERROR_CORRECT_M

#: 纠错级别。M（约 15%）在"屏幕反光 + 手机斜着扫"与"码不必太大"之间比较平衡；
#: 更高的 H 会让模块数涨一截，终端里反而难扫。
ERROR_CORRECTION = ERROR_CORRECT_M

#: SVG 里每个模块的像素边长。给管理页显示用，太小在手机上会糊。
SVG_BOX_PX = 10

#: 终端二维码的静区（模块数）。太小的话手机识别率明显下降；2 是常见最小值。
TERMINAL_BORDER = 2


def terminal_qr(text: str) -> str:
    """把文本渲染成终端可打印的二维码（含结尾换行）。

    **自己拼字符串，不用 ``QRCode.print_ascii``**：那个方法即使 ``tty=False`` 也会输出
    ``▀``（U+2580）这类半块字符，而 Windows 控制台默认 GBK，打印直接抛 ``UnicodeEncodeError``
    ——用户跑到 ``--pair`` 时崩在最后一步。这里只用 ``#`` 与空格，任何编码都能显示。

    每个模块画成两个字符宽：终端字符是 1:2 的竖长条，只画一个字符会让二维码被拉高成竖条，
    手机很难扫。
    """
    code = qrcode.QRCode(error_correction=ERROR_CORRECTION, border=TERMINAL_BORDER)
    code.add_data(text)
    code.make(fit=True)
    rows = ["".join("##" if cell else "  " for cell in row) for row in code.get_matrix()]
    return "\n".join(rows) + "\n"


def svg_qr(text: str) -> bytes:
    """把文本渲染成 SVG 二维码（矢量，管理页可直接内嵌或作为图片返回）。"""
    code = qrcode.QRCode(
        error_correction=ERROR_CORRECTION,
        box_size=SVG_BOX_PX,
        border=2,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    code.add_data(text)
    code.make(fit=True)
    image = code.make_image()
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


__all__ = ["svg_qr", "terminal_qr"]
