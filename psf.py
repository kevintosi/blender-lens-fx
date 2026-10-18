# SPDX-License-Identifier: GPL-3.0-or-later
"""
Physically based diffraction starburst.

The far-field (Fraunhofer) diffraction pattern of the lens aperture is the
squared magnitude of its Fourier transform. Polygonal apertures produce spikes
perpendicular to each blade edge: n spikes for an even blade count, 2n for odd.
Diffraction scales with wavelength, so the pattern is accumulated over the
visible spectrum, which gives the spikes their rainbow fringes.

The kernel is normalized and its central core removed (that light is already
in the rendered image), so it carries only the diffracted energy.
"""

import math

import numpy as np

KERNEL_RES = 512
_FFT_RES = 1024
_APERTURE_RADIUS = _FFT_RES / 5.0

# Wavelength samples (nm) with approximate sRGB-linear contributions,
# from a Gaussian fit of the CIE 1931 color matching functions.
_LAMBDAS = np.linspace(420.0, 680.0, 9)


def _cie_rgb(lam):
    def g(x, mu, s1, s2):
        s = s1 if x < mu else s2
        return math.exp(-0.5 * ((x - mu) / s) ** 2)

    x = 1.056 * g(lam, 599.8, 37.9, 31.0) + 0.362 * g(lam, 442.0, 16.0, 26.7) \
        - 0.065 * g(lam, 501.1, 20.4, 26.2)
    y = 0.821 * g(lam, 568.8, 46.9, 40.5) + 0.286 * g(lam, 530.9, 16.3, 31.1)
    z = 1.217 * g(lam, 437.0, 11.8, 36.0) + 0.681 * g(lam, 459.0, 26.0, 13.8)
    r = 3.2406 * x - 1.5372 * y - 0.4986 * z
    gg = -0.9689 * x + 1.8758 * y + 0.0415 * z
    b = 0.0557 * x - 0.2040 * y + 1.0570 * z
    return np.clip(np.array([r, gg, b]), 0.0, None)


_WEIGHTS = np.array([_cie_rgb(l) for l in _LAMBDAS])
_WEIGHTS /= _WEIGHTS.sum(axis=0, keepdims=True)  # spectrum sums to white


def _aperture(blades, rotation, imperfection, seed):
    n = _FFT_RES
    y, x = np.mgrid[0:n, 0:n].astype(np.float32)
    x -= n / 2
    y -= n / 2
    r = np.hypot(x, y)
    theta = np.arctan2(y, x) - rotation
    R = _APERTURE_RADIUS
    rng = np.random.default_rng(int(seed))
    if blades >= 3:
        seg = 2.0 * math.pi / blades
        local = np.mod(theta, seg) - seg / 2.0
        edge = R * math.cos(math.pi / blades) / np.cos(local)
    else:
        edge = np.full_like(r, R)
    if imperfection > 0.0:
        # Slightly uneven blade edges: low-frequency wobble around the rim.
        k = np.arange(1, 25)
        amp = rng.normal(0.0, 1.0, k.size) / k * 0.004 * R * imperfection
        phase = rng.uniform(0.0, 2.0 * math.pi, k.size)
        edge = edge + (amp[:, None, None] * np.cos(k[:, None, None] * theta + phase[:, None, None])).sum(0)
    a = np.clip(edge - r + 0.5, 0.0, 1.0)  # anti-aliased edge
    if imperfection > 0.0:
        # Dust on the glass near the stop scatters light into a faint fuzz.
        for _ in range(int(40 * imperfection)):
            cx, cy = rng.uniform(-R, R, 2)
            rad = rng.uniform(1.0, 4.0)
            a *= np.clip(np.hypot(x - cx, y - cy) - rad, 0.0, 1.0)
    return a


def _resample(img, scale):
    """Radially scale a centered image by `scale` (bilinear), preserving energy."""
    n = img.shape[0]
    c = n / 2.0
    idx = (np.arange(n, dtype=np.float32) - c) / scale + c
    i0 = np.clip(np.floor(idx).astype(np.int32), 0, n - 2)
    f = np.clip(idx - i0, 0.0, 1.0)
    valid = (idx >= 0) & (idx <= n - 1)
    rows = img[i0, :] * (1 - f)[:, None] + img[i0 + 1, :] * f[:, None]
    out = rows[:, i0] * (1 - f)[None, :] + rows[:, i0 + 1] * f[None, :]
    out *= valid[:, None] & valid[None, :]
    return out / (scale * scale)


def compute_kernel(blades, rotation, imperfection=0.3, seed=0):
    """Return a (KERNEL_RES, KERNEL_RES, 4) float32 RGBA starburst kernel."""
    a = _aperture(int(blades), float(rotation), float(imperfection), seed)
    f = np.fft.fftshift(np.fft.fft2(a))
    psf = (f.real ** 2 + f.imag ** 2).astype(np.float32)
    h = KERNEL_RES // 2
    c = _FFT_RES // 2
    psf = psf[c - h:c + h, c - h:c + h]
    psf /= psf.max()

    rgb = np.zeros((KERNEL_RES, KERNEL_RES, 3), dtype=np.float32)
    for lam, w in zip(_LAMBDAS, _WEIGHTS):
        p = _resample(psf, lam / 550.0)
        rgb += p[:, :, None] * w[None, None, :]

    # Remove the central core (first Airy lobe); keep the diffracted light.
    y, x = np.mgrid[0:KERNEL_RES, 0:KERNEL_RES].astype(np.float32) - h
    r = np.hypot(x, y)
    core = _FFT_RES / (2.0 * _APERTURE_RADIUS) * 1.6
    rgb *= (1.0 - np.exp(-(r / core) ** 2))[:, :, None]
    # Soft circular window so the kernel never shows a square edge.
    rgb *= np.clip((h - r) / (h * 0.25), 0.0, 1.0)[:, :, None] ** 2

    rgb /= rgb.sum(axis=(0, 1), keepdims=True) + 1e-12
    out = np.ones((KERNEL_RES, KERNEL_RES, 4), dtype=np.float32)
    out[:, :, :3] = rgb
    return out
