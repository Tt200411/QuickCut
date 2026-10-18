import pytest

from quickcut.encoder import FallbackEncoder, parse_ppm, ppm_to_png


@pytest.mark.parametrize("first", [9, 10, 13, 32])
def test_binary_ppm_preserves_whitespace_colored_first_pixel(first):
    pixels = bytes([first, 10, 32, 255, 0, 12])
    frame = b"P6\n2 1\n255\n" + pixels
    assert parse_ppm(frame) == (2, 1, pixels)
    assert len(FallbackEncoder().encode_image(frame)) == 72
    assert ppm_to_png(frame).startswith(b"\x89PNG")


def test_binary_ppm_rejects_truncated_raster():
    with pytest.raises(ValueError, match="raster length"):
        parse_ppm(b"P6\n1 1\n255\n\x00\x00")
