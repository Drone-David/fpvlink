/* fpvlut3d — a minimal 3D-LUT (.cube) color-grading element for FPVLink.
 *
 * Why this exists
 * ───────────────
 * GStreamer 1.28 on the RK3588 target ships no element that applies an
 * Adobe/Resolve .cube 3D LUT: there is no stock `lut3d`, no OpenGL plugins
 * (so no glshader), and gst-python isn't installed (so the element can't be
 * written in Python). ffmpeg has a `lut3d` filter but can only output to fbdev,
 * not the KMS/DRM plane the always-on display uses. So the HDMI LUT feature
 * needs its own tiny native element. This is it.
 *
 * What it does
 * ────────────
 * A GstVideoFilter over NV12 and the packed 8-bit RGB-family formats. Loads a
 * .cube file (LUT_3D_SIZE N, then N³ "r g b" triples, red varying fastest) and
 * applies it per pixel in place, with tetrahedral interpolation. Rows are split
 * across cores with OpenMP so 1080p60 stays real-time on the RK3588's 8 cores.
 *
 * Holding 60fps on this SoC took four things, each measured (see the NV12 note
 * further down for the numbers that motivated them):
 *   1. NV12 handled natively, so the display branch needs no videoconvert —
 *      three sequential full-frame passes collapse into this one.
 *   2. The 8-bit input's normalize→domain-map→scale chain precomputed into
 *      256-entry per-channel tables, off the hot loop entirely.
 *   3. Tetrahedral interpolation (4 corner fetches) instead of trilinear (8).
 *   4. Dynamic OpenMP scheduling, because the RK3588 is big.LITTLE and an
 *      equal static split leaves the frame waiting on the slow A55 cores.
 *
 * Property
 * ─────────
 *   file    : path to a .cube file. Parsed on set. If parsing fails the element
 *             degrades to passthrough (it never corrupts or drops frames) so a
 *             bad LUT can't take down the always-on display pipeline.
 *   enabled : grade or don't (default TRUE), switchable while PLAYING. Exists
 *             because the LUT sits after the input-selector, so it would
 *             otherwise regrade the synthetic standby card — ~1.6 cores of
 *             continuous work on a fanless box for a picture that needs no
 *             D-Log→709 conversion. pipeline.py clears it on the switch to
 *             standby and sets it again on the switch back to live.
 *
 * Build
 * ─────
 *   see setup/build-lut-plugin.sh (plain gcc; no meson needed).
 */

/* GST_PLUGIN_DEFINE expands PACKAGE (normally provided by a meson/autotools
 * config header). We build with a bare gcc call, so define it here. */
#ifndef PACKAGE
#define PACKAGE "fpvlink"
#endif

#include <gst/gst.h>
#include <gst/video/video.h>
#include <gst/video/gstvideofilter.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

GST_DEBUG_CATEGORY_STATIC (fpvlut3d_debug);
#define GST_CAT_DEFAULT fpvlut3d_debug

#define FPV_TYPE_LUT3D (fpv_lut3d_get_type())
G_DECLARE_FINAL_TYPE (FpvLut3d, fpv_lut3d, FPV, LUT3D, GstVideoFilter)

struct _FpvLut3d
{
  GstVideoFilter parent;

  gchar   *file;        /* path to .cube (property)                     */
  gboolean enabled;     /* property; FALSE → grade nothing, pass frames */
  gint     size;        /* LUT_3D_SIZE N; 0 → no LUT loaded (passthru)  */
  gfloat  *data;        /* N*N*N*3 floats, red-fastest ordering         */
  gfloat   dmin[3];     /* DOMAIN_MIN (default 0)                       */
  gfloat   dmax[3];     /* DOMAIN_MAX (default 1)                       */
  /* Per-channel 8-bit → LUT-index lookup, precomputed once per load.
   * The input is 8-bit, so the whole normalize→domain-map→clamp→scale
   * chain has only 256 possible outcomes per channel; baking it into these
   * tables removes 3 divisions + the domain math from every pixel (the hot
   * loop then only does the trilinear gather). idx[c][v] is the integer
   * base index (r0/g0/b0) and frc[c][v] the interpolation fraction. */
  gint    *idx[3];      /* [3][256], heap; NULL until a LUT is loaded   */
  gfloat  *frc[3];      /* [3][256], heap                               */
  GMutex   lock;        /* guards size/data/domain vs. property set     */

  /* ── NV12 fast path ──────────────────────────────────────────────────
   * When the negotiated format is NV12 the element does YUV→RGB, the LUT,
   * and RGB→YUV itself, in one pass, so the display branch needs no
   * videoconvert at all (see the NV12 note above fpv_lut3d_process_nv12).
   * All coefficients below are derived from the negotiated colorimetry in
   * set_info — never hardcoded — so BT.709/BT.601 and limited/full range
   * all match what videoconvert would have produced. */
  gboolean is_nv12;
  gfloat   ytab[256];   /* Y  8-bit → luma, full-range 0-255 float      */
  gfloat   utab[256];   /* Cb 8-bit → centred, scaled                   */
  gfloat   vtab[256];   /* Cr 8-bit → centred, scaled                   */
  gfloat   kr, kg, kb;  /* luma weights                                 */
  gfloat   cr, cb;      /* R += cr*v ; B += cb*u                        */
  gfloat   gu, gv;      /* G -= gu*u + gv*v                             */
  gfloat   iu, iv;      /* RGB→chroma normalisers                       */
  gfloat   yscale, yoff, cscale;   /* RGB→YUV range compression         */
};

enum { PROP_0, PROP_FILE, PROP_ENABLED };

/* NV12 first (preferred): it is what the hardware decoder emits and what the
 * DRM overlay plane accepts, so negotiating it lets this element sit directly
 * in the zero-copy display branch with no videoconvert on either side. The
 * packed 8-bit RGB layouts stay supported for any other use. */
#define FPVLUT3D_FORMATS "{ NV12, RGBx, BGRx, xRGB, xBGR, RGBA, BGRA, ARGB, ABGR, RGB, BGR }"

static GstStaticPadTemplate sink_template = GST_STATIC_PAD_TEMPLATE ("sink",
    GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS (GST_VIDEO_CAPS_MAKE (FPVLUT3D_FORMATS)));
static GstStaticPadTemplate src_template = GST_STATIC_PAD_TEMPLATE ("src",
    GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS (GST_VIDEO_CAPS_MAKE (FPVLUT3D_FORMATS)));

G_DEFINE_TYPE (FpvLut3d, fpv_lut3d, GST_TYPE_VIDEO_FILTER);

/* Put the element in GstBaseTransform passthrough whenever it has nothing to
 * do — disabled, or no usable LUT loaded. That skips the make-writable copy
 * base transform would otherwise do on every buffer (the display tee means
 * frames are never singly-referenced, so "in place" really means "copy first"),
 * which restores the zero-copy decoder→plane path while we're idle.
 *
 * Passthrough alone is NOT enough to stop the grading, though: base transform
 * still calls transform_ip on a passthrough element (it logs "doing passthrough
 * transform_ip"), and GstVideoFilter then maps the frame and calls
 * transform_frame_ip regardless. fpv_lut3d_transform_ip below is what actually
 * short-circuits the work. Both are needed. */
static void
fpv_lut3d_update_passthrough (FpvLut3d * self)
{
  gboolean have_lut;

  g_mutex_lock (&self->lock);
  have_lut = (self->data != NULL && self->size >= 2);
  g_mutex_unlock (&self->lock);

  gst_base_transform_set_passthrough (GST_BASE_TRANSFORM (self),
      !have_lut || !g_atomic_int_get (&self->enabled));
}

/* ── .cube parsing ─────────────────────────────────────────────────────────
 * Fills self->data (freshly allocated) and self->size on success. On any
 * failure leaves the element in passthrough (size = 0). Caller holds no lock;
 * this locks internally before publishing the result. */
static void
fpv_lut3d_load (FpvLut3d * self, const gchar * path)
{
  g_mutex_lock (&self->lock);
  g_clear_pointer (&self->data, g_free);
  for (gint c = 0; c < 3; c++) {
    g_clear_pointer (&self->idx[c], g_free);
    g_clear_pointer (&self->frc[c], g_free);
  }
  self->size = 0;
  self->dmin[0] = self->dmin[1] = self->dmin[2] = 0.0f;
  self->dmax[0] = self->dmax[1] = self->dmax[2] = 1.0f;
  g_mutex_unlock (&self->lock);

  if (!path || !*path)
    return;

  FILE *f = fopen (path, "r");
  if (!f) {
    GST_WARNING_OBJECT (self, "cannot open LUT '%s'", path);
    return;
  }

  gint n = 0;
  gfloat *data = NULL;
  gint filled = 0;           /* triples read so far */
  gint expected = 0;         /* n*n*n */
  gfloat dmin[3] = { 0, 0, 0 }, dmax[3] = { 1, 1, 1 };
  char line[512];

  while (fgets (line, sizeof (line), f)) {
    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '#' || *p == '\r' || *p == '\n' || *p == '\0')
      continue;

    if (g_ascii_strncasecmp (p, "TITLE", 5) == 0)
      continue;
    if (g_ascii_strncasecmp (p, "LUT_1D_SIZE", 11) == 0) {
      GST_WARNING_OBJECT (self, "1D LUTs are not supported; ignoring '%s'", path);
      break;
    }
    if (g_ascii_strncasecmp (p, "LUT_3D_SIZE", 11) == 0) {
      n = atoi (p + 11);
      if (n < 2 || n > 128) {   /* sane bound; 128³*3 floats ≈ 24 MB max */
        GST_WARNING_OBJECT (self, "bad LUT_3D_SIZE %d in '%s'", n, path);
        n = 0;
        break;
      }
      expected = n * n * n;
      data = g_try_malloc0 (sizeof (gfloat) * expected * 3);
      if (!data) { n = 0; break; }
      continue;
    }
    if (g_ascii_strncasecmp (p, "DOMAIN_MIN", 10) == 0) {
      sscanf (p + 10, "%f %f %f", &dmin[0], &dmin[1], &dmin[2]);
      continue;
    }
    if (g_ascii_strncasecmp (p, "DOMAIN_MAX", 10) == 0) {
      sscanf (p + 10, "%f %f %f", &dmax[0], &dmax[1], &dmax[2]);
      continue;
    }

    /* data row */
    if (data && filled < expected) {
      gfloat r, g, b;
      if (sscanf (p, "%f %f %f", &r, &g, &b) == 3) {
        data[filled * 3 + 0] = r;
        data[filled * 3 + 1] = g;
        data[filled * 3 + 2] = b;
        filled++;
      }
    }
  }
  fclose (f);

  if (!data || filled != expected) {
    GST_WARNING_OBJECT (self, "LUT '%s' incomplete (%d/%d entries) — passthrough",
        path, filled, expected);
    g_free (data);
    return;
  }

  /* Bake the per-channel 8-bit → (index, fraction) tables. Same math the hot
   * loop used to do per pixel, evaluated here once for each of the 256 input
   * values per channel. */
  gint *idx[3]; gfloat *frc[3];
  const gfloat scale = (gfloat) (n - 1);
  for (gint c = 0; c < 3; c++) {
    idx[c] = g_malloc (256 * sizeof (gint));
    frc[c] = g_malloc (256 * sizeof (gfloat));
    gfloat span = dmax[c] - dmin[c];
    gfloat inv = span != 0.f ? 1.f / span : 1.f;
    for (gint v = 0; v < 256; v++) {
      gfloat t = CLAMP ((((gfloat) v / 255.f) - dmin[c]) * inv, 0.f, 1.f) * scale;
      gint i = (gint) t;
      if (i > n - 1) i = n - 1;         /* guard the t == n-1 exact case */
      idx[c][v] = i;
      frc[c][v] = t - i;
    }
  }

  g_mutex_lock (&self->lock);
  self->data = data;
  self->size = n;
  memcpy (self->dmin, dmin, sizeof (dmin));
  memcpy (self->dmax, dmax, sizeof (dmax));
  for (gint c = 0; c < 3; c++) { self->idx[c] = idx[c]; self->frc[c] = frc[c]; }
  g_mutex_unlock (&self->lock);
  GST_INFO_OBJECT (self, "loaded %dx%dx%d LUT from '%s'", n, n, n, path);
}

/* ── colour-space setup ────────────────────────────────────────────────────
 * Called on every caps negotiation. Records whether we're on the NV12 fast
 * path and, if so, bakes the YUV↔RGB coefficients for the *negotiated*
 * colorimetry (matrix + range) so the result matches what videoconvert would
 * have produced for the same caps. */
static gboolean
fpv_lut3d_set_info (GstVideoFilter * filter, GstCaps * incaps,
    GstVideoInfo * in_info, GstCaps * outcaps, GstVideoInfo * out_info)
{
  FpvLut3d *self = FPV_LUT3D (filter);

  self->is_nv12 = (GST_VIDEO_INFO_FORMAT (in_info) == GST_VIDEO_FORMAT_NV12);
  if (!self->is_nv12)
    return TRUE;                /* packed-RGB path needs no coefficients */

  /* Luma weights from the negotiated matrix (BT.601 for SD-style caps,
   * BT.709 otherwise — the same default videoconvert applies). */
  gfloat kr, kb;
  switch (in_info->colorimetry.matrix) {
    case GST_VIDEO_COLOR_MATRIX_BT601:
      kr = 0.299f; kb = 0.114f; break;
    case GST_VIDEO_COLOR_MATRIX_BT2020:
      kr = 0.2627f; kb = 0.0593f; break;
    case GST_VIDEO_COLOR_MATRIX_BT709:
    default:
      kr = 0.2126f; kb = 0.0722f; break;
  }
  gfloat kg = 1.0f - kr - kb;
  self->kr = kr; self->kg = kg; self->kb = kb;

  /* Decode (YUV→RGB) and encode (RGB→YUV) constants. */
  self->cr = 2.0f * (1.0f - kr);
  self->cb = 2.0f * (1.0f - kb);
  self->gu = 2.0f * kb * (1.0f - kb) / kg;
  self->gv = 2.0f * kr * (1.0f - kr) / kg;
  self->iu = 1.0f / (2.0f * (1.0f - kb));
  self->iv = 1.0f / (2.0f * (1.0f - kr));

  /* Range: limited (16-235 luma / 16-240 chroma) unless caps say full. */
  gboolean full = (in_info->colorimetry.range == GST_VIDEO_COLOR_RANGE_0_255);
  gfloat ydec = full ? 1.0f : 255.0f / 219.0f;
  gfloat yzero = full ? 0.0f : 16.0f;
  gfloat cdec = full ? 1.0f : 255.0f / 224.0f;
  for (gint i = 0; i < 256; i++) {
    self->ytab[i] = ((gfloat) i - yzero) * ydec;
    self->utab[i] = ((gfloat) i - 128.0f) * cdec;
    self->vtab[i] = ((gfloat) i - 128.0f) * cdec;
  }
  self->yscale = full ? 1.0f : 219.0f / 255.0f;
  self->yoff   = full ? 0.0f : 16.0f;
  self->cscale = full ? 1.0f : 224.0f / 255.0f;

  GST_INFO_OBJECT (self, "NV12 fast path: matrix=%d range=%s (kr=%.4f kb=%.4f)",
      in_info->colorimetry.matrix, full ? "full" : "limited", kr, kb);
  return TRUE;
}

/* Round-and-clamp a 0-255 float to a byte. */
static inline guint8
fpv_f2b (gfloat v)
{
  return (guint8) CLAMP (v + 0.5f, 0.f, 255.f);
}

/* Trilinear sample. r0/g0/b0 are the integer base indices and dr/dg/db the
 * interpolation fractions — both precomputed from the 8-bit input via the
 * per-channel tables (see fpv_lut3d_load). out[0..2] receive interpolated
 * normalized RGB. */
static inline void
fpv_lut3d_sample (const gfloat * lut, gint n, gint r0, gfloat dr,
    gint g0, gfloat dg, gint b0, gfloat db, gfloat * out)
{
  gint r1 = MIN (r0 + 1, n - 1), g1 = MIN (g0 + 1, n - 1), b1 = MIN (b0 + 1, n - 1);

#define IDX(ri, gi, bi) ((((bi) * n + (gi)) * n + (ri)) * 3)
  const gfloat *c000 = lut + IDX (r0, g0, b0);
  const gfloat *c111 = lut + IDX (r1, g1, b1);
  const gfloat *ca, *cb2;
  gfloat wa, wb, wc;
#define TETRA(A, B, WA, WB, WC) \
  do { ca = lut + IDX A; cb2 = lut + IDX B; wa = WA; wb = WB; wc = WC; } while (0)

  /* Tetrahedral interpolation: the cell is split into 6 tetrahedra and only
   * the 4 corners of the one containing (dr,dg,db) are read — half the
   * gathers of trilinear, which is what buys the frame budget here. It is
   * also the interpolation professional LUT tooling (OCIO, Resolve, ffmpeg's
   * lut3d) uses by default, so the grade matches what the .cube was authored
   * against. The ordering of dr/dg/db picks the tetrahedron. */
  if (dr > dg) {
    if (dg > db)                /* r > g > b */
      TETRA ((r1, g0, b0), (r1, g1, b0), dr, dg, db);
    else if (dr > db)           /* r > b > g */
      TETRA ((r1, g0, b0), (r1, g0, b1), dr, db, dg);
    else                        /* b > r > g */
      TETRA ((r0, g0, b1), (r1, g0, b1), db, dr, dg);
  } else {
    if (db > dg)                /* b > g > r */
      TETRA ((r0, g0, b1), (r0, g1, b1), db, dg, dr);
    else if (db > dr)           /* g > b > r */
      TETRA ((r0, g1, b0), (r0, g1, b1), dg, db, dr);
    else                        /* g > r > b */
      TETRA ((r0, g1, b0), (r1, g1, b0), dg, dr, db);
  }
#undef TETRA
#undef IDX

  /* out = c000 + wa*(ca-c000) + wb*(cb2-ca) + wc*(c111-cb2) */
  for (gint k = 0; k < 3; k++) {
    out[k] = c000[k]
        + wa * (ca[k] - c000[k])
        + wb * (cb2[k] - ca[k])
        + wc * (c111[k] - cb2[k]);
  }
}

/* ── NV12 in-place grade ───────────────────────────────────────────────────
 * Why this exists: the display branch is NV12 end to end (hardware decoder →
 * DRM overlay plane 194). Bracketing an RGB-only LUT with videoconvert cost
 * three sequential full-frame passes — and videoconvert is single-threaded,
 * so those two passes alone exceeded the 16.7 ms frame budget and stalled the
 * display. Doing YUV→RGB, the LUT, and RGB→YUV inline here collapses all
 * three into the one already-parallel loop.
 *
 * Both quantisation steps below are deliberate, not sloppy: the old path
 * wrote 8-bit RGB out of videoconvert and again out of the LUT, so rounding
 * at exactly those two points is what keeps the output identical to it.
 *
 * Chroma is 4:2:0 — one UV pair per 2x2 luma block — so each block is graded
 * as four pixels sharing that pair, and the four resulting chromas are
 * averaged back into it. */
static void
fpv_lut3d_process_nv12 (FpvLut3d * self, GstVideoFrame * frame,
    const gfloat * lut, gint n)
{
  const gint width = GST_VIDEO_FRAME_WIDTH (frame);
  const gint height = GST_VIDEO_FRAME_HEIGHT (frame);
  const gint ystride = GST_VIDEO_FRAME_PLANE_STRIDE (frame, 0);
  const gint cstride = GST_VIDEO_FRAME_PLANE_STRIDE (frame, 1);
  guint8 *ybase = GST_VIDEO_FRAME_PLANE_DATA (frame, 0);
  guint8 *cbase = GST_VIDEO_FRAME_PLANE_DATA (frame, 1);

  const gfloat *ytab = self->ytab, *utab = self->utab, *vtab = self->vtab;
  const gint *idxR = self->idx[0], *idxG = self->idx[1], *idxB = self->idx[2];
  const gfloat *frcR = self->frc[0], *frcG = self->frc[1], *frcB = self->frc[2];
  const gfloat kr = self->kr, kg = self->kg, kb = self->kb;
  const gfloat crr = self->cr, cbb = self->cb, gu = self->gu, gv = self->gv;
  const gfloat iu = self->iu, iv = self->iv;
  const gfloat yscale = self->yscale, yoff = self->yoff, cscale = self->cscale;

  /* Round up so odd sizes still cover the last row/column (indices clamp). */
  const gint bh = (height + 1) / 2, bw = (width + 1) / 2;

  /* Dynamic, not static: the RK3588 is big.LITTLE (4x A76 @2.26GHz + 4x A55
   * @1.8GHz). An equal static split makes the A55 threads stragglers and the
   * whole frame waits on them; dynamic chunks let the A76s absorb the slack.
   * A chunk of 8 block-rows keeps scheduling overhead negligible. */
#ifdef _OPENMP
#pragma omp parallel for schedule (dynamic, 8)
#endif
  for (gint by = 0; by < bh; by++) {
    const gint y0 = by * 2, y1 = MIN (y0 + 1, height - 1);
    guint8 *row0 = ybase + (gsize) y0 * ystride;
    guint8 *row1 = ybase + (gsize) y1 * ystride;
    guint8 *crow = cbase + (gsize) by * cstride;

    for (gint bx = 0; bx < bw; bx++) {
      const gint x0 = bx * 2, x1 = MIN (x0 + 1, width - 1);
      const gfloat u = utab[crow[bx * 2 + 0]];
      const gfloat v = vtab[crow[bx * 2 + 1]];
      /* Chroma-derived terms are shared by all four pixels in the block. */
      const gfloat rv = crr * v, bu = cbb * u, guv = gu * u + gv * v;

      guint8 *yp[4] = { row0 + x0, row0 + x1, row1 + x0, row1 + x1 };
      /* Snapshot the four luma samples before writing any of them: on an odd
       * width or height the clamps above make x1==x0 / y1==y0, so two of these
       * pointers alias, and grading in place would grade that edge pixel twice. */
      const guint8 ysrc[4] = { *yp[0], *yp[1], *yp[2], *yp[3] };
      gfloat su = 0.f, sv = 0.f;

      for (gint i = 0; i < 4; i++) {
        const gfloat yy = ytab[ysrc[i]];
        /* YUV → RGB, quantised to 8-bit exactly as videoconvert did. */
        const guint8 r8 = fpv_f2b (yy + rv);
        const guint8 g8 = fpv_f2b (yy - guv);
        const guint8 b8 = fpv_f2b (yy + bu);

        gfloat out[3];
        fpv_lut3d_sample (lut, n, idxR[r8], frcR[r8], idxG[g8], frcG[g8],
            idxB[b8], frcB[b8], out);

        /* Second quantisation point — matches the old 8-bit LUT output. */
        const gfloat R = fpv_f2b (out[0] * 255.f);
        const gfloat G = fpv_f2b (out[1] * 255.f);
        const gfloat B = fpv_f2b (out[2] * 255.f);

        const gfloat Yl = kr * R + kg * G + kb * B;
        *yp[i] = fpv_f2b (Yl * yscale + yoff);
        su += (B - Yl);
        sv += (R - Yl);
      }

      crow[bx * 2 + 0] = fpv_f2b (su * 0.25f * iu * cscale + 128.f);
      crow[bx * 2 + 1] = fpv_f2b (sv * 0.25f * iv * cscale + 128.f);
    }
  }
}

static GstFlowReturn
fpv_lut3d_transform_frame_ip (GstVideoFilter * filter, GstVideoFrame * frame)
{
  FpvLut3d *self = FPV_LUT3D (filter);

  g_mutex_lock (&self->lock);
  gint n = self->size;
  const gfloat *lut = self->data;
  const gint *idxR = self->idx[0], *idxG = self->idx[1], *idxB = self->idx[2];
  const gfloat *frcR = self->frc[0], *frcG = self->frc[1], *frcB = self->frc[2];
  g_mutex_unlock (&self->lock);

  if (!lut || n < 2)
    return GST_FLOW_OK;         /* passthrough */

  if (self->is_nv12) {
    fpv_lut3d_process_nv12 (self, frame, lut, n);
    return GST_FLOW_OK;
  }

  const gint width = GST_VIDEO_FRAME_WIDTH (frame);
  const gint height = GST_VIDEO_FRAME_HEIGHT (frame);
  const gint stride = GST_VIDEO_FRAME_PLANE_STRIDE (frame, 0);
  guint8 *base = GST_VIDEO_FRAME_PLANE_DATA (frame, 0);

  /* Byte offset of each colour component within a pixel, and the pixel stride,
   * derived from the negotiated format — handles RGBx/BGRx/RGB/… uniformly. */
  const gint ro = GST_VIDEO_FRAME_COMP_POFFSET (frame, 0);
  const gint go = GST_VIDEO_FRAME_COMP_POFFSET (frame, 1);
  const gint bo = GST_VIDEO_FRAME_COMP_POFFSET (frame, 2);
  const gint ps = GST_VIDEO_FRAME_COMP_PSTRIDE (frame, 0);

#ifdef _OPENMP
#pragma omp parallel for schedule (static)
#endif
  for (gint y = 0; y < height; y++) {
    guint8 *row = base + (gsize) y * stride;
    for (gint x = 0; x < width; x++) {
      guint8 *px = row + (gsize) x * ps;
      guint8 rv = px[ro], gv = px[go], bv = px[bo];

      gfloat out[3];
      fpv_lut3d_sample (lut, n, idxR[rv], frcR[rv], idxG[gv], frcG[gv],
          idxB[bv], frcB[bv], out);

      px[ro] = (guint8) CLAMP (out[0] * 255.f + 0.5f, 0.f, 255.f);
      px[go] = (guint8) CLAMP (out[1] * 255.f + 0.5f, 0.f, 255.f);
      px[bo] = (guint8) CLAMP (out[2] * 255.f + 0.5f, 0.f, 255.f);
    }
  }

  return GST_FLOW_OK;
}

/* The real gate for `enabled`. GstVideoFilter's transform_ip maps the frame
 * before it ever reaches transform_frame_ip, and base transform calls it even
 * in passthrough — so returning early has to happen here, above the map, or a
 * disabled element still pays a full-frame READWRITE dmabuf mapping per buffer.
 * Chains up to GstVideoFilter for the normal (grading) path. */
static GstFlowReturn
fpv_lut3d_transform_ip (GstBaseTransform * base, GstBuffer * buf)
{
  FpvLut3d *self = FPV_LUT3D (base);

  if (!g_atomic_int_get (&self->enabled))
    return GST_FLOW_OK;

  return GST_BASE_TRANSFORM_CLASS (fpv_lut3d_parent_class)->transform_ip (base,
      buf);
}

/* ── GObject boilerplate ───────────────────────────────────────────────── */
static void
fpv_lut3d_set_property (GObject * obj, guint id, const GValue * val,
    GParamSpec * pspec)
{
  FpvLut3d *self = FPV_LUT3D (obj);
  switch (id) {
    case PROP_FILE:
      g_free (self->file);
      self->file = g_value_dup_string (val);
      fpv_lut3d_load (self, self->file);
      fpv_lut3d_update_passthrough (self);
      break;
    case PROP_ENABLED:
      /* Atomic because the streaming thread reads it per buffer without the
       * mutex — a stale read costs at most one wrongly-graded frame. */
      g_atomic_int_set (&self->enabled, g_value_get_boolean (val));
      fpv_lut3d_update_passthrough (self);
      break;
    default:
      G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, id, pspec);
  }
}

static void
fpv_lut3d_get_property (GObject * obj, guint id, GValue * val, GParamSpec * pspec)
{
  FpvLut3d *self = FPV_LUT3D (obj);
  switch (id) {
    case PROP_FILE:
      g_value_set_string (val, self->file);
      break;
    case PROP_ENABLED:
      g_value_set_boolean (val, g_atomic_int_get (&self->enabled));
      break;
    default:
      G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, id, pspec);
  }
}

static void
fpv_lut3d_finalize (GObject * obj)
{
  FpvLut3d *self = FPV_LUT3D (obj);
  g_free (self->file);
  g_free (self->data);
  for (gint c = 0; c < 3; c++) { g_free (self->idx[c]); g_free (self->frc[c]); }
  g_mutex_clear (&self->lock);
  G_OBJECT_CLASS (fpv_lut3d_parent_class)->finalize (obj);
}

static void
fpv_lut3d_init (FpvLut3d * self)
{
  g_mutex_init (&self->lock);
  self->dmax[0] = self->dmax[1] = self->dmax[2] = 1.0f;
  self->enabled = TRUE;
  /* No LUT yet, so start bypassed; loading a file re-evaluates this. */
  gst_base_transform_set_passthrough (GST_BASE_TRANSFORM (self), TRUE);
}

static void
fpv_lut3d_class_init (FpvLut3dClass * klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);
  GstElementClass *element_class = GST_ELEMENT_CLASS (klass);
  GstBaseTransformClass *btrans_class = GST_BASE_TRANSFORM_CLASS (klass);
  GstVideoFilterClass *vfilter_class = GST_VIDEO_FILTER_CLASS (klass);

  gobject_class->set_property = fpv_lut3d_set_property;
  gobject_class->get_property = fpv_lut3d_get_property;
  gobject_class->finalize = fpv_lut3d_finalize;

  g_object_class_install_property (gobject_class, PROP_FILE,
      g_param_spec_string ("file", "File", "Path to a .cube 3D LUT file",
          NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));

  g_object_class_install_property (gobject_class, PROP_ENABLED,
      g_param_spec_boolean ("enabled", "Enabled",
          "Apply the LUT. Set FALSE to pass frames through untouched — used to "
          "skip grading the synthetic standby card, which costs ~1.6 cores to "
          "regrade for no benefit. Safe to toggle while playing.",
          TRUE, G_PARAM_READWRITE | GST_PARAM_MUTABLE_PLAYING |
          G_PARAM_STATIC_STRINGS));

  gst_element_class_set_static_metadata (element_class,
      "FPVLink 3D LUT", "Filter/Effect/Video",
      "Applies a .cube 3D LUT with trilinear interpolation",
      "FPVLink");

  gst_element_class_add_static_pad_template (element_class, &sink_template);
  gst_element_class_add_static_pad_template (element_class, &src_template);

  btrans_class->transform_ip = fpv_lut3d_transform_ip;
  vfilter_class->set_info = fpv_lut3d_set_info;
  vfilter_class->transform_frame_ip = fpv_lut3d_transform_frame_ip;
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  GST_DEBUG_CATEGORY_INIT (fpvlut3d_debug, "fpvlut3d", 0, "FPVLink 3D LUT");
  return gst_element_register (plugin, "fpvlut3d", GST_RANK_NONE,
      FPV_TYPE_LUT3D);
}

/* The license field takes one of a fixed set of strings GStreamer knows.
 * FPVLink is under PolyForm Noncommercial 1.0.0, which is source-available
 * but not a free-software license, so none of the free-license values apply
 * and "Proprietary" is the honest bucket. See LICENSE. */
GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR,
    fpvlut3d, "FPVLink 3D LUT color grading",
    plugin_init, "1.0", "Proprietary", "fpvlink", "https://github.com/Drone-David/fpvlink")
