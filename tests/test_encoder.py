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


def test_fallback_accepts_ingestion_png_without_changing_embedding():
    pytest.importorskip("PIL")
    ppm = b"P6\n2 1\n255\n" + bytes([20, 150, 20, 30, 170, 30])
    encoder = FallbackEncoder()
    assert encoder.encode_image(ppm_to_png(ppm)) == encoder.encode_image(ppm)


def test_fallback_rejects_corrupt_encoded_keyframe():
    pytest.importorskip("PIL")
    with pytest.raises((ValueError, OSError)):
        FallbackEncoder().encode_image(b"\x89PNG\r\n\x1a\nbroken")
