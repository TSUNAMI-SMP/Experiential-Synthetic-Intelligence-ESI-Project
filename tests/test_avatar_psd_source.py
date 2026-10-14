import importlib.util
from pathlib import Path

import pytest
from PIL import Image

script = Path(__file__).resolve().parents[1] / "scripts/build-avatar-psd.py"
spec = importlib.util.spec_from_file_location("avatar_psd_source", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_psd_independent_reader_preserves_layer_pixels_visibility_and_reference_preview(tmp_path):
    reader = pytest.importorskip("psd_tools")
    reference = tmp_path / "reference.png"
    prototype = tmp_path / "prototype.png"
    original = Image.new("RGBA", (3, 2), (140, 80, 60, 255))
    original.putpixel((0, 0), (20, 30, 40, 0))
    original.putpixel((1, 0), (140, 80, 60, 128))
    draft = Image.new("RGBA", (3, 2), (80, 90, 100, 128))
    original.save(reference)
    draft.save(prototype)
    destination = module.build_psd(tmp_path / "source.psd", reference, [("Face_prototype", prototype)])
    document = reader.PSDImage.open(destination)
    assert document.size == (3, 2) and len(document) == 2
    assert document[0].name == "Face_prototype" and not document[0].visible
    assert document[1].name == "Original_reference" and document[1].visible
    assert document[0].topil().tobytes() == draft.tobytes()
    assert document[1].topil().tobytes() == original.tobytes()
    preview = document.topil()
    assert preview.getchannel("A").tobytes() == original.getchannel("A").tobytes()
    assert preview.getpixel((0, 0)) == original.getpixel((0, 0))
    assert preview.getpixel((2, 0)) == original.getpixel((2, 0))
    # Eight-bit white-matted previews quantize semitransparent colors, but must
    # retain their appearance. Raw source layer pixels above remain exact.
    background = Image.new("RGBA", original.size, (40, 70, 100, 255))
    expected = Image.alpha_composite(background, original).tobytes()
    actual = Image.alpha_composite(background, preview).tobytes()
    assert max(abs(a - b) for a, b in zip(actual, expected, strict=True)) <= 1


def test_source_writer_rejects_mismatched_canvas_and_preserves_existing_psd(tmp_path):
    reference, prototype = tmp_path / "reference.png", tmp_path / "prototype.png"
    Image.new("RGBA", (3, 2)).save(reference)
    Image.new("RGBA", (4, 2)).save(prototype)
    with pytest.raises(ValueError, match="同じキャンバス"):
        module.build_psd(tmp_path / "bad.psd", reference, [("Face", prototype)])
    assert not (tmp_path / "bad.psd").exists()
    destination = tmp_path / "artist.psd"
    destination.write_bytes(b"existing artist work")
    with pytest.raises(FileExistsError):
        module.build_psd(destination, reference, [])
    assert destination.read_bytes() == b"existing artist work"
