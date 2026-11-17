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


@pytest.mark.parametrize("mode", ["openclip", "mobileclip"])
def test_explicit_model_failure_never_returns_color_fallback(monkeypatch, mode):
    import quickcut.encoder as module
    monkeypatch.setattr(module, "_ENCODER_CACHE", {})
    def unavailable():
        raise RuntimeError("missing real weights")
    monkeypatch.setattr(module, "OpenClipEncoder", unavailable)
    with pytest.raises(RuntimeError, match="missing real weights"):
        module.make_encoder(mode)


def test_auto_failure_is_disclosed_and_not_cached(monkeypatch, capsys):
    import quickcut.encoder as module
    monkeypatch.setattr(module, "_ENCODER_CACHE", {})
    def unavailable():
        raise RuntimeError("offline checkpoint missing")
    monkeypatch.setattr(module, "OpenClipEncoder", unavailable)
    fallback = module.make_encoder("auto")
    assert fallback.profile()["requested_mode"] == "auto"
    assert fallback.profile()["fallback_reason"] == "offline checkpoint missing"
    output = capsys.readouterr()
    assert output.out == "" and "offline checkpoint missing" in output.err
    restored = object()
    monkeypatch.setattr(module, "OpenClipEncoder", lambda: restored)
    assert module.make_encoder("auto") is restored


def test_encoder_cache_tracks_checkpoint_and_text_model_selection(monkeypatch):
    import quickcut.encoder as module
    monkeypatch.setattr(module, "_ENCODER_CACHE", {})
    monkeypatch.setattr(module, "OpenClipEncoder", object)
    monkeypatch.setenv("QUICKCUT_CLIP_PRETRAINED", "checkpoint-a")
    first = module.make_encoder("openclip")
    assert module.make_encoder("openclip") is first
    monkeypatch.setenv("QUICKCUT_CLIP_PRETRAINED", "checkpoint-b")
    second = module.make_encoder("openclip")
    assert second is not first
    monkeypatch.setenv("QUICKCUT_CLIP_TEXT_MODEL", "pinned-tokenizer")
    assert module.make_encoder("openclip") is not second


def test_unknown_encoder_cannot_silently_select_fallback():
    from quickcut.encoder import make_encoder
    with pytest.raises(ValueError, match="Unknown visual encoder"):
        make_encoder("openclpi")
