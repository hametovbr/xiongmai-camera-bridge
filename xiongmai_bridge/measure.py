"""Measure how far the picture moved between two frames (phase correlation).

Used to verify that a preset really returns the head to the same view: the
camera answers Ret 100 even when nothing moved, so the frame is the only
reliable acceptance check.

    dx, dy, peak = shift(reference, current)

Values are in pixels of the given frames (the bridge uses the 640x360
sub-stream). peak is the correlation strength: two frames of the same still
scene give ~0.7, a peak below ~0.1 means the views barely overlap.

Requires numpy and Pillow; frames can be grabbed with PyAV (see grab_frame).
"""

import numpy as np


def _prepare(img):
    a = np.asarray(img.convert("L"), dtype=float)
    h, w = a.shape
    # cut OSD lines (time on top, title at the bottom) and the vignetted border
    a = a[int(h * 0.15):int(h * 0.85), int(w * 0.05):int(w * 0.95)]
    a = a - a.mean()
    return a * np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))


def shift(reference, current):
    """Offset of `current` relative to `reference`: (dx, dy, peak)."""
    a, b = _prepare(reference), _prepare(current)
    cross = np.fft.fft2(a) * np.conj(np.fft.fft2(b))
    corr = np.fft.ifft2(cross / (np.abs(cross) + 1e-9)).real
    y, x = np.unravel_index(corr.argmax(), corr.shape)
    h, w = corr.shape
    if y > h // 2:
        y -= h
    if x > w // 2:
        x -= w
    return int(x), int(y), round(float(corr.max()), 3)


def grab_frame(rtsp_url, skip=3, timeout=10):
    """Decode one frame from an RTSP URL (PyAV), skipping the first `skip` frames."""
    import av

    container = av.open(rtsp_url, options={"rtsp_transport": "tcp"}, timeout=timeout)
    try:
        for n, frame in enumerate(container.decode(container.streams.video[0])):
            if n >= skip:
                return frame.to_image()
    finally:
        container.close()
    raise RuntimeError("no frame received")


def rtsp_url(host, user, password, stream=1, port=554):
    """Xiongmai RTSP URL: credentials go into the path, stream 0 = main, 1 = sub."""
    return (f"rtsp://{host}:{port}/user={user}_password={password}"
            f"_channel=1_stream={stream}.sdp?real_stream")
