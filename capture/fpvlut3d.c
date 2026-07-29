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
 * A GstVideoFilter over packed 8-bit RGB-family formats. Loads a .cube file
 * (LUT_3D_SIZE N, then N³ "r g b" triples, red varying fastest) and applies it
 * per pixel with trilinear interpolation, in place. Rows are split across cores
 * with OpenMP so 1080p60 stays real-time on the RK3588's 8 cores.
 *
 * Property
 * ────────
 *   file : path to a .cube file. Parsed on set. If parsing fails the element
 *          degrades to passthrough (it never corrupts or drops frames) so a bad
 *          LUT can't take down the always-on display pipeline.
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
  gint     size;        /* LUT_3D_SIZE N; 0 → no LUT loaded (passthru)  */
  gfloat  *data;        /* N*N*N*3 floats, red-fastest ordering         */
  gfloat   dmin[3];     /* DOMAIN_MIN (default 0)                       */
  gfloat   dmax[3];     /* DOMAIN_MAX (default 1)                       */
  GMutex   lock;        /* guards size/data/domain vs. property set     */
};

enum { PROP_0, PROP_FILE };

/* Accept the common packed 8-bit RGB layouts. videoconvert on either side
 * bridges to/from the decoder's NV12 and the display's NV12. */
#define FPVLUT3D_FORMATS "{ RGBx, BGRx, xRGB, xBGR, RGBA, BGRA, ARGB, ABGR, RGB, BGR }"

static GstStaticPadTemplate sink_template = GST_STATIC_PAD_TEMPLATE ("sink",
    GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS (GST_VIDEO_CAPS_MAKE (FPVLUT3D_FORMATS)));
static GstStaticPadTemplate src_template = GST_STATIC_PAD_TEMPLATE ("src",
    GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS (GST_VIDEO_CAPS_MAKE (FPVLUT3D_FORMATS)));

G_DEFINE_TYPE (FpvLut3d, fpv_lut3d, GST_TYPE_VIDEO_FILTER);

/* ── .cube parsing ─────────────────────────────────────────────────────────
 * Fills self->data (freshly allocated) and self->size on success. On any
 * failure leaves the element in passthrough (size = 0). Caller holds no lock;
 * this locks internally before publishing the result. */
static void
fpv_lut3d_load (FpvLut3d * self, const gchar * path)
{
  g_mutex_lock (&self->lock);
  g_clear_pointer (&self->data, g_free);
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

  g_mutex_lock (&self->lock);
  self->data = data;
  self->size = n;
  memcpy (self->dmin, dmin, sizeof (dmin));
  memcpy (self->dmax, dmax, sizeof (dmax));
  g_mutex_unlock (&self->lock);
  GST_INFO_OBJECT (self, "loaded %dx%dx%d LUT from '%s'", n, n, n, path);
}

/* Trilinear sample. rf/gf/bf are normalized [0,1] (already domain-mapped).
 * out[0..2] receive interpolated normalized RGB. */
static inline void
fpv_lut3d_sample (const gfloat * lut, gint n, gfloat rf, gfloat gf, gfloat bf,
    gfloat * out)
{
  const gfloat scale = (gfloat) (n - 1);
  gfloat fr = CLAMP (rf, 0.f, 1.f) * scale;
  gfloat fg = CLAMP (gf, 0.f, 1.f) * scale;
  gfloat fb = CLAMP (bf, 0.f, 1.f) * scale;

  gint r0 = (gint) fr, g0 = (gint) fg, b0 = (gint) fb;
  gint r1 = MIN (r0 + 1, n - 1), g1 = MIN (g0 + 1, n - 1), b1 = MIN (b0 + 1, n - 1);
  gfloat dr = fr - r0, dg = fg - g0, db = fb - b0;

#define IDX(ri, gi, bi) ((((bi) * n + (gi)) * n + (ri)) * 3)
  const gfloat *c000 = lut + IDX (r0, g0, b0);
  const gfloat *c100 = lut + IDX (r1, g0, b0);
  const gfloat *c010 = lut + IDX (r0, g1, b0);
  const gfloat *c110 = lut + IDX (r1, g1, b0);
  const gfloat *c001 = lut + IDX (r0, g0, b1);
  const gfloat *c101 = lut + IDX (r1, g0, b1);
  const gfloat *c011 = lut + IDX (r0, g1, b1);
  const gfloat *c111 = lut + IDX (r1, g1, b1);
#undef IDX

  for (gint k = 0; k < 3; k++) {
    gfloat x00 = c000[k] + dr * (c100[k] - c000[k]);
    gfloat x10 = c010[k] + dr * (c110[k] - c010[k]);
    gfloat x01 = c001[k] + dr * (c101[k] - c001[k]);
    gfloat x11 = c011[k] + dr * (c111[k] - c011[k]);
    gfloat y0 = x00 + dg * (x10 - x00);
    gfloat y1 = x01 + dg * (x11 - x01);
    out[k] = y0 + db * (y1 - y0);
  }
}

static GstFlowReturn
fpv_lut3d_transform_frame_ip (GstVideoFilter * filter, GstVideoFrame * frame)
{
  FpvLut3d *self = FPV_LUT3D (filter);

  g_mutex_lock (&self->lock);
  gint n = self->size;
  gfloat *lut = self->data;
  gfloat dmin0 = self->dmin[0], dmin1 = self->dmin[1], dmin2 = self->dmin[2];
  gfloat dspan0 = self->dmax[0] - dmin0;
  gfloat dspan1 = self->dmax[1] - dmin1;
  gfloat dspan2 = self->dmax[2] - dmin2;
  g_mutex_unlock (&self->lock);

  if (!lut || n < 2)
    return GST_FLOW_OK;         /* passthrough */

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

  const gfloat inv0 = dspan0 != 0.f ? 1.f / dspan0 : 1.f;
  const gfloat inv1 = dspan1 != 0.f ? 1.f / dspan1 : 1.f;
  const gfloat inv2 = dspan2 != 0.f ? 1.f / dspan2 : 1.f;

#ifdef _OPENMP
#pragma omp parallel for schedule (static)
#endif
  for (gint y = 0; y < height; y++) {
    guint8 *row = base + (gsize) y * stride;
    for (gint x = 0; x < width; x++) {
      guint8 *px = row + (gsize) x * ps;
      gfloat rf = ((px[ro] / 255.f) - dmin0) * inv0;
      gfloat gf = ((px[go] / 255.f) - dmin1) * inv1;
      gfloat bf = ((px[bo] / 255.f) - dmin2) * inv2;

      gfloat out[3];
      fpv_lut3d_sample (lut, n, rf, gf, bf, out);

      px[ro] = (guint8) CLAMP (out[0] * 255.f + 0.5f, 0.f, 255.f);
      px[go] = (guint8) CLAMP (out[1] * 255.f + 0.5f, 0.f, 255.f);
      px[bo] = (guint8) CLAMP (out[2] * 255.f + 0.5f, 0.f, 255.f);
    }
  }

  return GST_FLOW_OK;
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
  g_mutex_clear (&self->lock);
  G_OBJECT_CLASS (fpv_lut3d_parent_class)->finalize (obj);
}

static void
fpv_lut3d_init (FpvLut3d * self)
{
  g_mutex_init (&self->lock);
  self->dmax[0] = self->dmax[1] = self->dmax[2] = 1.0f;
}

static void
fpv_lut3d_class_init (FpvLut3dClass * klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);
  GstElementClass *element_class = GST_ELEMENT_CLASS (klass);
  GstVideoFilterClass *vfilter_class = GST_VIDEO_FILTER_CLASS (klass);

  gobject_class->set_property = fpv_lut3d_set_property;
  gobject_class->get_property = fpv_lut3d_get_property;
  gobject_class->finalize = fpv_lut3d_finalize;

  g_object_class_install_property (gobject_class, PROP_FILE,
      g_param_spec_string ("file", "File", "Path to a .cube 3D LUT file",
          NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));

  gst_element_class_set_static_metadata (element_class,
      "FPVLink 3D LUT", "Filter/Effect/Video",
      "Applies a .cube 3D LUT with trilinear interpolation",
      "FPVLink");

  gst_element_class_add_static_pad_template (element_class, &sink_template);
  gst_element_class_add_static_pad_template (element_class, &src_template);

  vfilter_class->transform_frame_ip = fpv_lut3d_transform_frame_ip;
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  GST_DEBUG_CATEGORY_INIT (fpvlut3d_debug, "fpvlut3d", 0, "FPVLink 3D LUT");
  return gst_element_register (plugin, "fpvlut3d", GST_RANK_NONE,
      FPV_TYPE_LUT3D);
}

GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR,
    fpvlut3d, "FPVLink 3D LUT color grading",
    plugin_init, "1.0", "LGPL", "fpvlink", "https://github.com/fpvlink/fpvlink")
