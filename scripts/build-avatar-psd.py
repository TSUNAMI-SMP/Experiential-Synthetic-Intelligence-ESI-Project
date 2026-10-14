"""Package original/generated RGBA PNGs into PSD layers without altering their pixels.

Prototype layers remain hidden; the original reference is the merged preview.
Registration, clean-up and Cubism rigging are subsequent editor operations.
"""

import argparse
import struct
from pathlib import Path

from PIL import Image


def number(code, *values):
    return struct.pack(">" + code, *values)


def read_rgba(path):
    with Image.open(path) as image:
        if image.mode != "RGBA":
            raise ValueError("素材にはRGBA形式のPNGを使ってください。")
        if image.format != "PNG" or max(image.size) > 4096:
            raise ValueError("素材は最大4096ピクセルのPNGにしてください。")
        return image.size, image.tobytes()


def build_psd(destination, reference, prototypes):
    if len(prototypes) > 31:
        raise ValueError("この制作素材の書き出しは32レイヤーまでです。")
    size, original = read_rgba(reference)
    width, height = size
    if width * height * 4 * (len(prototypes) + 2) > 128_000_000:
        raise ValueError("制作素材のPSDが大きすぎます。キャンバスかレイヤー数を減らしてください。")
    layers = []
    names = set()
    # PSD stores the topmost layer first; all generated prototypes are initially hidden.
    for name, path in reversed(prototypes):
        if name in names or name == "Original_reference":
            raise ValueError("レイヤー名が重複しています。")
        names.add(name)
        layer_size, pixels = read_rgba(path)
        if layer_size != size:
            raise ValueError("すべての素材を同じキャンバスサイズにしてください。")
        layers.append((name, pixels, True))
    layers.append(("Original_reference", original, False))
    records, image_data = bytearray(), bytearray()
    for name, pixels, hidden in layers:
        encoded = name.encode("ascii")
        if not encoded or len(encoded) > 255:
            raise ValueError("レイヤー名は255文字以内の英数字にしてください。")
        pascal = bytes([len(encoded)]) + encoded
        pascal += bytes((-len(pascal)) % 4)
        extra = number("II", 0, 0) + pascal
        records += number("iiiiH", 0, 0, height, width, 4)
        for channel_id in (0, 1, 2, -1):
            records += number("hI", channel_id, 2 + width * height)
        records += b"8BIMnorm" + bytes((255, 0, 2 if hidden else 0, 0))
        records += number("I", len(extra)) + extra
        for channel in range(4):
            image_data += number("H", 0) + pixels[channel::4]
    # Negative layer count declares transparency in the merged image.
    layer_info = number("h", -len(layers)) + records + image_data
    layer_info += bytes(len(layer_info) % 2)
    masks = number("I", len(layer_info)) + layer_info + number("I", 0)
    header = b"8BPS" + number("H", 1) + bytes(6) + number("HIIHH", 4, height, width, 8, 3)
    # Cubism's PSD importer requires an image resource block. ResolutionInfo
    # is a standard Photoshop resource (72 pixels/inch; dimensions in inches).
    resolution = number("IHHIHH", 72 << 16, 1, 1, 72 << 16, 1, 1)
    resources = b"8BIM" + number("H", 1005) + bytes(2) + number("I", len(resolution)) + resolution
    # PSD's merged RGB preview is encoded against white; layer channels retain
    # the exact straight RGBA source pixels. Decoders undo this preview matte.
    alpha = original[3::4]
    preview_channels = [
        bytes((value * opacity + 255 * (255 - opacity) + 127) // 255 if opacity else value
              for value, opacity in zip(original[channel::4], alpha, strict=True))
        for channel in range(3)
    ]
    merged = number("H", 0) + b"".join([*preview_channels, alpha])
    content = header + number("I", 0) + number("I", len(resources)) + resources
    content += number("I", len(masks)) + masks + merged
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("既存のPSDを保持しました。新しい保存先を選んでください。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an existing artist's file, including a concurrent creation.
    with destination.open("xb") as output:
        output.write(content)
    return destination


def main():
    parser = argparse.ArgumentParser(description="RGBA素材を編集用PSDへ保存（モデル制作前の素材）")
    parser.add_argument("--reference", required=True)
    parser.add_argument("--layer", action="append", default=[], metavar="NAME=PNG")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    prototypes = [tuple(item.split("=", 1)) for item in args.layer]
    if any(len(item) != 2 for item in prototypes):
        parser.error("--layer は NAME=PNG の形式にしてください。")
    print(build_psd(args.output, args.reference, prototypes))


if __name__ == "__main__":
    main()
