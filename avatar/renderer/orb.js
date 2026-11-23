// The orb form (cfg.form 'orb'): instead of the VRM character, a black orb with an electric blue glow, drawn by one
// fragment shader over the window, so it costs the GPU a little and the CPU next to nothing. orb-motion.js decides
// what it looks like each frame; this draws it, speaks the same protocol as app.js, and leaves the window's
// choreography (flying out to point, home again) to the main process as it is for her.
//
// It breathes when idle; her surface churns and ripples with her voice and her motes stream out as she talks; while
// you talk a halo breathes round her and the motes stream into her; thinking, currents of light wind through her;
// while a tool runs beads of light race along her wisps; a feeling lifts her motes (or lets them sink) and livens or
// calms her. Pointing, she flies to the thing leaving a fading trail of light along her path, turns into a small
// comet-like pointer with light running down it to the tip, and a living ring round the thing, and back into an orb
// as she leaves. All of it is her state's numbers driving one shader, nothing canned.
//
// Another form (a mascot, say) would be a module like this one, picked by cfg.form in app.js.

import { GHOST_S, MAX_RIPPLES, ORB_METRICS, OrbMotion, TRAIL_POINTS, TRAIL_S } from './orb-motion.js';
import { SeeThrough } from './seethrough.js';

const MAX_DT = 0.1;
// About 60 frames a second whatever the display, evenly paced: every display frame on a 60 Hz monitor, every other
// one on a 120-144 Hz one (a fixed 1000/60 ms gate on 144 Hz lands on every third frame, 48 fps, and judders).
const TARGET_MS = 1000 / 60;
const MAX_DPR = 2; // a sharper canvas than this only costs power
const MOTES = 22; // sparks drifting round her
const WISPS = 7; // rings of light on tilted orbits round her
// PS2-style frame blending: each frame keeps a fading copy of the last (the brighter of the two wins), so fast
// light leaves a short glow behind it instead of stepping. This much of a frame's light is left after a second's
// fraction: exp(-dt / BLEND_TAU) a frame, so it looks the same at any frame rate.
export const BLEND_TAU = 0.02;

const VERTEX = `
attribute vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

// The two passes after her scene (which draws into a texture): the blend with the frame before, and the screen.
const BLEND = `
precision highp float;
uniform sampler2D uNow;
uniform sampler2D uThen;
uniform vec2 uRes;
uniform float uKeep;
void main() {
  vec2 uv = gl_FragCoord.xy / uRes;
  // Faded a little past the multiply, so the last glimmer of a ghost goes out instead of sticking at one 8-bit level.
  vec4 then = max(texture2D(uThen, uv) * uKeep - 1.5 / 255.0, vec4(0.0));
  gl_FragColor = max(texture2D(uNow, uv), then);
}
`;

// The PS2-era screen: scanlines (every other row darker), a faint bright band rolling slowly down, then the colours
// cut to a few levels with an ordered (Bayer) dither, as old console hardware drew its gradients.
const PRESENT = `
precision highp float;
uniform sampler2D uImage;
uniform vec2 uRes;
uniform float uDpr;
uniform float uTime;

// The 4x4 Bayer matrix's threshold (0..1) at pixel a, without integer maths (GLSL ES 1.0).
float bayer2(vec2 a) {
  a = floor(a);
  return fract(dot(a, vec2(0.5, a.y * 0.75)));
}
float bayer4(vec2 a) {
  return bayer2(0.5 * a) * 0.25 + bayer2(a);
}

void main() {
  vec4 c = texture2D(uImage, gl_FragCoord.xy / uRes);
  vec2 fc = gl_FragCoord.xy / max(1.0, floor(uDpr + 0.5));
  float row = step(0.5, fract(fc.y * 0.5));
  float roll = exp(-pow((fract(gl_FragCoord.y / uRes.y + uTime * 0.07) - 0.5) * 9.0, 2.0));
  // No interlace flicker, and a gentle roll: the user found the flicker too strong. Gentle rows: a bright thread
  // drifting across darker rows strobed at 28% contrast. The roll is barely there: it moves all day long.
  float f = (0.8 + 0.2 * row) * (1.0 + 0.035 * roll);
  float m = max(c.r, max(c.g, c.b));
  vec3 light = c.rgb * f;
  // Her glow darkens with the rows; her translucent body stays as solid as it was.
  float a = clamp(max(c.a * f, c.a - m * (1.0 - f)), 0.0, 1.0);
  // Fine enough steps that a slowly changing glow doesn't visibly pop from one level to the next. Faint light isn't
  // stepped at all: dithered near nothing, pixels at a moving glow's edge popped on and off.
  vec3 stepped = max(floor(light * 48.0 + bayer4(fc)) / 48.0, vec3(0.0));
  light = mix(light, stepped, smoothstep(0.04, 0.15, max(light.r, max(light.g, light.b))));
  gl_FragColor = vec4(min(light, vec3(a)), a);
}
`;

// Window CSS px, y down. Output is premultiplied: the window is transparent everywhere she isn't.
const FRAGMENT = `
precision highp float;
uniform vec2 uRes;
uniform float uDpr;
uniform float uTime;
uniform vec2 uCentre;
uniform float uRadius;
uniform float uAlpha;
uniform vec3 uStretch;
uniform float uGlow;
uniform float uSwirl;
uniform float uShimmer;
uniform float uSpin;
uniform float uHalo;
uniform float uOrbit;
uniform float uHue;
uniform float uVoice;
uniform float uKick;
uniform vec2 uLook;
uniform float uMorph;
uniform vec2 uRipples[${MAX_RIPPLES}];
uniform vec4 uRing;
uniform float uRingRound;
uniform float uRingAlpha;
uniform float uRingFlash;
uniform float uRingDraw;
uniform vec2 uTip;
uniform float uFlow;
uniform float uSurface;
uniform float uLift;
uniform float uEnergy;
uniform vec3 uTrail[${TRAIL_POINTS}];
uniform float uForm;
uniform float uDrowse;
uniform float uGather;
uniform vec4 uGhost;

const float TAU = 6.2831853;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}

float fbm(vec2 p) {
  float v = 0.5 * noise(p);
  v += 0.25 * noise(p * 2.03 + 7.1);
  v += 0.125 * noise(p * 4.01 + 3.7);
  return v / 0.875;
}

vec3 tint(float h) {
  vec3 blue = vec3(0.10, 0.45, 1.0);
  vec3 cyan = vec3(0.30, 0.88, 1.0);
  vec3 violet = vec3(0.48, 0.32, 1.0);
  return h >= 0.0 ? mix(blue, cyan, h) : mix(blue, violet, -h);
}

float roundRect(vec2 p, vec2 extent, float r) {
  vec2 q = abs(p) - extent + r;
  return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

// Distance to a segment a -> b whose thickness tapers from ra (at a) to rb (at b).
float taper(vec2 p, vec2 a, vec2 b, float ra, float rb) {
  vec2 ab = b - a;
  float t = clamp(dot(p - a, ab) / max(dot(ab, ab), 1e-4), 0.0, 1.0);
  return length(p - a - ab * t) - mix(ra, rb, t);
}

void main() {
  vec2 p = vec2(gl_FragCoord.x, uRes.y - gl_FragCoord.y) / uDpr;
  vec3 light = vec3(0.0);
  float core = 0.0;
  vec3 col = tint(uHue + 0.12 * uLift); // a light feeling leans cyan, a heavy one violet
  vec3 white = vec3(0.85, 0.95, 1.0);

  // ---- the orb ----
  if (uAlpha > 0.001 && uRadius > 0.1) {
    vec2 q = p - uCentre;
    // Everything round her fades out before the window's edge, so no glow is ever cut off square.
    vec2 win = uRes / uDpr;
    float toEdge = min(min(p.x, p.y), min(win.x - p.x, win.y - p.y));
    // ...and, at home, falls off round her well inside the window's circle: faded only near the square's sides,
    // the glow still ended in a visible square on a light desktop (the user saw it cut off).
    float halfWin = 0.5 * min(win.x, win.y);
    float edge = smoothstep(0.0, max(8.0, 0.35 * uRadius), toEdge)
      * (1.0 - smoothstep(0.6 * halfWin, 0.97 * halfWin, length(q)));
    // Stretched along her flight: longer that way, thinner across.
    float along = dot(q, uStretch.xy);
    vec2 across = q - along * uStretch.xy;
    float s = uStretch.z;
    vec2 qs = across * (1.0 + 0.35 * s) + uStretch.xy * along / (1.0 + s);
    float r = length(qs) / uRadius;
    // Her faint outer glow eases out before the r < 4.5 cut below: cut off there, it showed as a faint ring, and
    // it jumped out to the window's edge the moment she set off flying (that cut opens up for her tail).
    float reach = 1.0 - smoothstep(3.2, 4.4, r);
    if (r < 4.5 || (s > 0.05 && along < 0.0 && length(across) < uRadius * 2.0)) {
      float ang = atan(qs.y, qs.x + 1e-5); // never atan(0, 0)
      // Noise is sampled round a circle, never by the angle itself: atan jumps on her left, and that showed as a seam.
      vec2 dir = qs / max(length(qs), 1e-4);
      // The edge trembles with her voice (two waves against each other, so it never sets into a polygon), and
      // shimmers while she thinks.
      // Her voice churns the surface: noise round her edge travelling with uSurface (sampled round a circle by it, so
      // its wrap never shows), quicker as she talks, over two slow waves.
      vec2 sc = vec2(cos(uSurface), sin(uSurface));
      vec2 sc2 = vec2(cos(2.0 * uSurface), sin(2.0 * uSurface));
      float churn = (noise(dir * 2.2 + 3.0 * sc) - 0.5) * 0.11 + (noise(dir * 4.3 + 2.0 * sc2.yx + 9.0) - 0.5) * 0.05;
      float wob = uVoice * (churn + 0.016 * sin(ang * 3.0 + uTime * 7.0) + 0.012 * sin(ang * 5.0 - uTime * 11.0))
        + uShimmer * 0.012 * sin(ang * 7.0 - uTime * 6.0)
        + uShimmer * 0.016 * (noise(dir * 1.8 + vec2(uTime * 2.0, -uTime * 1.3)) - 0.5);
      float d = r - (1.0 + wob * (1.0 - uMorph));
      float px = 1.2 / uRadius;
      float body = 1.0 - smoothstep(-px, px, d);
      core = body * 0.85; // she's translucent: the desktop shows faintly through her
      float orbit = 1.0 - uMorph; // what belongs to the orb and not the pointer fades as she turns into one
      vec3 hot = mix(col, white, 0.55); // the white-blue of the brightest light

      // The look is the user's PS2-era reference: a translucent blue sphere with a white-hot heart, grainy like an
      // old console's texture, wrapped in wisps of light on tilted orbits, under scanlines (at the end of main).
      float rn = clamp(r / (1.0 + wob * orbit), 0.0, 1.0);
      float nz = sqrt(max(0.0, 1.0 - rn * rn));
      float fresnel = pow(1.0 - nz, 2.2);

      // Her heart: white-hot in the middle, falling off through blue; it swells with her voice.
      float heartLevel = (0.75 + 0.35 * uGlow + 0.5 * uVoice + 0.5 * uKick) * (1.0 + 0.18 * min(uLift, 0.0) + 0.1 * uEnergy)
        * (1.0 + 1.4 * uGather); // gathered into a point to cross the screen, she burns white
      // Filled, not hollow: a broad glow fills the whole sphere round the hot centre.
      float heart = (exp(-r * r * 6.0) * 1.0 + exp(-r * r * 2.0) * 0.55 + body * 0.18) * heartLevel;
      // A star flare on her heart, turning slowly and pulsing: the lens flare every PS2 effect had.
      float fa = uTime * 0.12;
      vec2 fq = mat2(cos(fa), sin(fa), -sin(fa), cos(fa)) * (q / uRadius);
      float flare = exp(-abs(fq.x) * 26.0) * exp(-abs(fq.y) * 1.6) + exp(-abs(fq.y) * 26.0) * exp(-abs(fq.x) * 1.6);
      vec2 fd = mat2(0.7071, 0.7071, -0.7071, 0.7071) * fq;
      flare += 0.45 * (exp(-abs(fd.x) * 34.0) * exp(-abs(fd.y) * 3.2) + exp(-abs(fd.y) * 34.0) * exp(-abs(fd.x) * 3.2));
      flare *= (0.55 + 0.12 * sin(uTime * 0.6) + 0.5 * uVoice + 0.4 * uKick) * heartLevel * 0.42 * (1.0 - 0.4 * uDrowse) * uForm * uForm;
      // The sphere: dim translucent blue, brighter toward its rim, with a fine animated grain and a slow swirl of
      // light inside (quicker and brighter while thinking), sampled through the sphere so it reads as depth.
      float twist = uSpin * 2.0 + r * 2.5 * uSwirl;
      vec2 swirled = mat2(cos(twist), sin(twist), -sin(twist), cos(twist)) * (qs / uRadius) / (0.55 + 0.45 * nz);
      float sw = fbm(swirled * 1.7 + vec2(0.0, -uTime * 0.35));
      float grain = noise(qs * 0.55 + vec2(uTime * 0.5, -uTime * 0.35)); // a speck every couple of pixels, drifting slowly
      float shell = body * (0.3 + 0.45 * fresnel + 0.08 * grain + pow(sw, 2.5) * (0.15 + 0.7 * uSwirl)); // (less at rest: her fog below fills it)
      // Thinking: currents of light winding through her, the ridges of a slowly flowing noise inside the sphere
      // (wider toward her rim, where the sphere is thin, so they never shrink under a pixel and flicker).
      float currents = 0.0;
      if (uSwirl > 0.2 && body > 0.01) {
        float f = fbm(swirled * 1.25 + vec2(uTime * 0.13, -uTime * 0.09) + 4.0);
        float ridge = 1.0 - smoothstep(0.0, 0.035 + 0.04 * (1.0 - nz), abs(f - 0.5));
        float drift = smoothstep(0.35, 0.75, noise(swirled * 0.9 + vec2(-uTime * 0.2, uTime * 0.15) + 11.0));
        currents = ridge * (0.25 + 0.75 * drift) * body * nz * smoothstep(0.2, 0.8, uSwirl) * 0.85;
      }
      // A soft rim, no crisp line: it should glow, not gleam.
      float rimLevel = 0.45 + 0.5 * uGlow + 0.5 * uVoice;
      float rimBand = exp(-abs(d) * 10.0) * rimLevel * 0.6;

      // Glass: her rim's light split faintly into cyan just outside it and lavender just inside, the two colours bent
      // apart as old CGI drew glass. Wider on the side away from a light that wanders round her slowly, so it never
      // sits as an even outline.
      vec2 lightDir = vec2(cos(uTime * 0.07 + 0.8), sin(uTime * 0.07 + 0.8));
      float away = 0.55 + 0.45 * dot(dir, -lightDir);
      float split = 0.045 * away;
      float fringeC = exp(-abs(d - split) * 18.0);
      float fringeL = exp(-abs(d + 1.4 * split) * 18.0);
      vec3 glass = (vec3(0.30, 0.86, 1.0) * fringeC + vec3(0.64, 0.54, 1.0) * fringeL) * (0.32 + 0.12 * uGlow + 0.12 * uVoice) * away;

      // Lit fog inside her: three nested shells of drifting noise, each seen where the line of sight crosses it in
      // front and behind, turning round her upright axis at a pace of its own, so the layers slide past each other
      // (that parallax is what reads as depth). Her heart lights it, the inner shells most. Faded out smoothly where
      // the line of sight grazes a shell: faded linearly, the inner shell's edge showed as a disc round her heart.
      float mist = 0.0;
      if (body > 0.01) {
        vec2 sq = qs / uRadius;
        for (int k = 0; k < 3; k++) {
          float fk = float(k);
          float rk = 0.4 + 0.24 * fk;
          float zz = rk * rk - rn * rn;
          if (zz <= 0.0) continue;
          float z = sqrt(zz);
          float turn = uTime * (0.05 + 0.025 * fk) * (fk == 1.0 ? -1.0 : 1.0) + fk * 2.1;
          float ct = cos(turn);
          float st = sin(turn);
          float scale = 2.0 + 0.7 * fk;
          vec2 rise = vec2(fk * 13.1, uTime * 0.04 + fk * 7.7);
          float front = noise(vec2(sq.x * ct + z * st, sq.y) * scale + rise);
          float back = noise(vec2(sq.x * ct - z * st, sq.y) * scale + rise + 5.3);
          float dens = smoothstep(0.42, 0.85, front) + 0.55 * smoothstep(0.42, 0.85, back);
          float graze = zz / (rk * rk); // 1 straight through .. 0 grazing
          mist += dens * graze * graze * exp(-rk * rk * 1.8); // squared: no kink, so no circle where the shell ends
        }
        mist *= body * (0.26 + 0.1 * uGlow + 0.22 * uVoice + 0.1 * uSwirl) * (1.0 + 0.1 * uEnergy) * (1.0 - 0.3 * uDrowse);
      }

      // Ghosts: now and then (OrbMotion's uGhost, a random 40-100 s apart, only at rest) faint geometry surfaces in
      // the glass for a few seconds and sinks away again: a ring, a polygon or an arc on a tilted plane, turning
      // slowly. Its line wavers with noise fixed on the screen and is broken into runs by noise in a frame of its
      // own, so it reads as an afterimage in the glass rather than a picture turning (the user's rule for every
      // ring), and it carries the glass's cyan and lavender split. Thinking, it shows a little more.
      float ghost = 0.0;
      float ghostC = 0.0;
      float ghostL = 0.0;
      float gAge = uGhost.x;
      if (gAge >= 0.0 && gAge < 1.0 && body > 0.01) {
        float g1 = uGhost.w;
        float g2 = fract(g1 * 7.13 + 0.31);
        float g3 = fract(g1 * 3.71 + 0.57);
        float g4 = fract(g1 * 13.7 + 0.19);
        float vis = smoothstep(0.0, 0.35, gAge) * (1.0 - smoothstep(0.55, 1.0, gAge));
        float ga = uTime * (0.22 + 0.18 * g4) * (g4 > 0.5 ? 1.0 : -1.0) + g1 * 40.0;
        vec2 gp = mat2(cos(ga), -sin(ga), sin(ga), cos(ga)) * (qs / uRadius);
        float tilt = mix(0.4, 0.95, g3);
        gp.y /= tilt;
        float size = 0.48 + 0.22 * g2 + 0.04 * sin(uTime * 0.9 + g3 * 6.0);
        float lg = max(length(gp), 1e-4);
        vec2 gn = gp / lg;
        float sd = lg - size; // a ring, signed: outside positive
        float kind = uGhost.y;
        if (kind == 1.0) {
          // A polygon of 3 to 6 sides (atan's jump on her left is a whole number of sides, so no seam).
          float n = uGhost.z;
          float seg = TAU / n;
          float a2 = mod(atan(gp.y, gp.x + 1e-5) + 0.5 * seg, seg) - 0.5 * seg;
          sd = lg * cos(a2) - 0.85 * size; // its sides about where the ring would be
        }
        float span = 1.0;
        if (kind == 2.0) {
          // An arc: a stretch of ring that drifts round it.
          float aa = uTime * 0.3 + g2 * TAU;
          span = smoothstep(-0.2, 0.5, dot(gn, vec2(cos(aa), sin(aa))));
        }
        sd = sd * uRadius * mix(tilt, 1.0, abs(gn.x)) + 2.0 * (noise(dir * 2.5 + vec2(uTime * 0.6, g1 * 9.0)) - 0.5);
        float gpsi = uTime * 0.45 * (g2 > 0.5 ? 1.0 : -1.0);
        vec2 gf = mat2(cos(gpsi), -sin(gpsi), sin(gpsi), cos(gpsi)) * gn;
        float gruns = 0.3 + 0.7 * smoothstep(0.3, 0.8, noise(gf * 1.8 + vec2(g4 * 7.0, uTime * 0.5)));
        // Hidden toward her rim (it's in the glass, not on it) and wherever it turns behind her heart's glare.
        float gLevel = vis * gruns * span * body * (1.0 - smoothstep(0.7, 0.98, rn)) * (0.28 + 0.15 * uSwirl) * (1.0 - 0.3 * uDrowse);
        ghost = exp(-sd * sd / 1.7) * gLevel;
        ghostC = exp(-(sd - 1.1) * (sd - 1.1) / 1.3) * gLevel;
        ghostL = exp(-(sd + 1.1) * (sd + 1.1) / 1.3) * gLevel;
      }

      // Wisps: ribbons of light on orbits round her that never sit still: each one's tilt swings (it precesses),
      // its pace drifts, its path ripples and breathes, light flows along it in bright runs, and it fades out and
      // back in over its own few seconds. Each is two strands that twist round each other, like a ribbon. They're
      // hidden where they pass behind her.
      float wisps = 0.0;
      vec2 v0 = qs / uRadius;
      for (int k = 0; k < ${WISPS}; k++) {
        if (r > 2.2 + 1.6 * (1.0 - uForm)) break; // none reach this far out
        float fk = float(k);
        float h1 = hash(vec2(fk, 3.3));
        float h2 = hash(vec2(fk, 7.7));
        float h3 = hash(vec2(fk, 1.9));
        float way = h3 > 0.5 ? 1.0 : -1.0;
        float cosT = mix(0.18, 0.95, 0.5 + 0.5 * sin(uTime * (0.11 + 0.17 * h1) + h2 * TAU)); // the tilt swings
        // Quicker while she thinks or works through uSpin, the spin integrated over time (OrbMotion). Never time
        // times a rate that changes with her state: that jumped the phase by hundreds of radians as she changed
        // state, and the wisps snapped. Each one's share of the spin is a multiple of 1/4, so the spin's wrap
        // (SPIN_WRAP, 8 pi) turns it by whole turns.
        float share = 0.5 + 0.25 * floor(h3 * 4.0);
        float phi = h2 * TAU + way * (uTime * (0.06 + 0.08 * h3) + uSpin * share
          + 0.7 * sin(uTime * (0.23 + 0.2 * h2) + h1 * 9.0));
        vec2 v = mat2(cos(phi), -sin(phi), sin(phi), cos(phi)) * v0;
        v.y /= cosT;
        float lv = max(length(v), 1e-4);
        vec2 nv = v / lv;
        // Its path: a size of its own that breathes, bent by noise fixed on the screen (so its shape changes as it
        // turns through it, rather than a rigid ring turning) and a little noise of its own. With all of it carried
        // by the ring, it read as a picture spinning (the user's "static pngs rotating").
        vec2 wd = v0 / max(length(v0), 1e-4);
        float R = (1.0 + 0.8 * (1.0 - uForm) * (0.6 + 0.4 * h1)) * (1.05 + 0.3 * h3) + 0.06 * sin(uTime * (0.5 + 0.4 * h1) + fk * 2.1) + 0.06 * uVoice
          + 0.13 * (noise(wd * 2.0 + vec2(fk * 3.1, uTime * 0.5)) - 0.5)
          + 0.07 * (noise(nv * 2.6 + vec2(fk * 1.7, uTime * 0.8)) - 0.5);
        // Where light runs along it: its own frame turned on at a pace of its own, so the bright runs and the
        // thick and thin stretches travel along the ring instead of riding round with it.
        float psi = way * uTime * (0.5 + 0.5 * h2);
        vec2 nf = mat2(cos(psi), -sin(psi), sin(psi), cos(psi)) * nv;
        float squash = mix(cosT, 1.0, abs(nv.x)); // distances to the flattened orbit, back in pixels
        // Two strands twisting round its path.
        float twistK = sin(dot(nv, vec2(2.0, 1.3)) * 3.0 + uTime * (1.4 + h2) * way) * (0.018 + 0.02 * h1);
        float d1 = abs(lv - R - twistK) * uRadius * squash;
        float d2 = abs(lv - R + twistK) * uRadius * squash;
        // Mostly fine threads, a few broad soft bands; never under about a pixel and a quarter, or a thread moving
        // across the pixels flickers from one frame to the next.
        float width = (1.25 + 1.6 * h2 * h2) * (0.7 + 0.6 * noise(nf * 2.3 + vec2(fk * 5.0, uTime * 0.2)));
        // Light flowing along it in bright runs, and the whole wisp fading out and back in now and then.
        float run = smoothstep(0.3, 0.85, noise(nf * 1.7 + vec2(h1 * 9.0, uTime * 0.35))) * (1.0 + 0.35 * uEnergy);
        // While she works, beads of light race along it (cos of twice the angle: no seam where atan wraps).
        float around = atan(nv.y, nv.x + 1e-5);
        float bead = pow(max(0.0, cos(2.0 * around - way * uTime * (2.2 + h1))), 18.0) * uOrbit;
        float lifePhase = fract(uTime * (0.045 + 0.04 * h3) + h1);
        float life = smoothstep(0.0, 0.25, lifePhase) * (1.0 - smoothstep(0.7, 1.0, lifePhase));
        float behind = mix(mix(1.0, 0.3, body), 1.0, smoothstep(-0.15, 0.15, nv.y)); // eased: a step notched them
        float strands = exp(-d1 * d1 / (width * width)) + 0.6 * exp(-d2 * d2 / (width * width * 0.6));
        wisps += (strands * 0.62 + exp(-min(d1, d2) * 0.3) * 0.14) * (0.2 + 0.8 * run + 1.6 * bead) * (0.15 + 0.85 * life) * behind;
      }
      wisps *= (0.55 + 0.45 * uGlow + 0.5 * uVoice + 0.4 * uOrbit) * (1.0 - 0.35 * uDrowse) * orbit * edge;

      // The bloom round her: a tight glow and a wide one, faded out before the window's edge.
      float out_ = max(d, 0.0);
      float bloomLevel = (0.35 + 0.65 * uGlow + 0.6 * uVoice + 0.8 * uKick) * (1.0 - 0.2 * uDrowse);
      float glow = (exp(-out_ * 6.0) * 0.55 + exp(-out_ * (2.4 - 0.8 * uGlow)) * 0.4) * bloomLevel * (1.0 - body) * edge * reach;
      // Haze: a faint fog of her light hanging round her, thicker in drifting patches, like the air round a PS2
      // light source.
      float haze = 0.0;
      if (out_ < 2.6) {
        float fog = fbm(q / uRadius * 0.85 + vec2(uTime * 0.045, -uTime * 0.03));
        haze = smoothstep(0.3, 0.75, fog) * exp(-out_ * 1.15) * (1.0 - body) * edge * reach * (0.16 + 0.14 * uGlow + 0.12 * uVoice);
      }
      // A comet tail behind her while she flies.
      float tail = 0.0;
      if (s > 0.05 && along < 0.0) {
        float w = uRadius * (0.5 + 0.2 * s);
        tail = exp(-length(across) / w * 1.6) * exp(along / (uRadius * (1.5 + 4.0 * s))) * min(s, 1.0) * 0.9;
        // Faded in from her middle and kept off her face: cut off at along = 0, it split her in two.
        tail *= smoothstep(0.0, uRadius * 0.8, -along) * (1.0 - body);
      }
      light += col * (shell + rimBand + glow + tail + heart * 0.6) + mix(col, white, 0.3) * wisps
        + mix(col, white, 0.45) * currents * orbit + mix(col, vec3(0.55, 0.65, 0.8), 0.5) * haze
        + white * (heart * 0.55 + flare * edge) * orbit + hot * tail * 0.3
        + (glass + mix(col, white, 0.3) * mist + mix(col, white, 0.5) * ghost * 0.6
          + vec3(0.30, 0.86, 1.0) * ghostC * 0.5 + vec3(0.64, 0.54, 1.0) * ghostL * 0.5) * orbit * edge;

      // Ripples racing out with her syllables, gone before the window's edge.
      for (int i = 0; i < ${MAX_RIPPLES}; i++) {
        vec2 rp = uRipples[i];
        if (rp.y <= 0.0) continue;
        float age = rp.x / 0.9;
        float fi = float(i);
        // Not a perfect circle: bent by noise round it, and brighter in some stretches than others.
        float at = 1.0 + age * 0.85 + 0.07 * (noise(dir * 2.4 + vec2(fi * 7.3, uTime * 0.6)) - 0.5) * (0.5 + age);
        float stretch = 0.45 + 0.55 * noise(dir * 1.8 + vec2(fi * 3.9 + uTime * 0.4, 1.7));
        // Faded in as it sets off: at full strength from its first frame, each one snapped on.
        float band = exp(-abs(r - at) * (30.0 - 16.0 * age)) * smoothstep(0.0, 0.2, age) * (1.0 - age) * (1.0 - age) * rp.y * stretch;
        light += mix(col, hot, 0.3) * band * 0.9 * orbit * edge;
      }

      // Listening: a halo breathing round her, made like the wisps (two strands, bent by noise that drifts, light
      // running round it), not a drawn circle.
      if (uHalo > 0.01) {
        float hr = mix(1.0, 1.24, uHalo) + 0.03 * sin(uTime * 3.0) + 0.05 * (noise(dir * 2.2 + vec2(uTime * 0.4, 5.3)) - 0.5);
        float hk = 0.015 * sin(dot(dir, vec2(2.0, 1.3)) * 3.0 + uTime * 1.3);
        float hd1 = abs(r - hr - hk) * uRadius;
        float hd2 = abs(r - hr + hk) * uRadius;
        float hrun = 0.35 + 0.65 * smoothstep(0.25, 0.8, noise(mat2(cos(uTime * 0.7), -sin(uTime * 0.7), sin(uTime * 0.7), cos(uTime * 0.7)) * dir * 1.6 + vec2(2.1, uTime * 0.3)));
        float halo = (exp(-hd1 * hd1 / 2.6) + 0.6 * exp(-hd2 * hd2 / 1.6) + 0.12 * exp(-min(hd1, hd2) * 0.25)) * hrun * uHalo;
        light += mix(col, white, 0.35) * halo * 0.8 * orbit * edge;
      }

      // Working shows on the wisps themselves (beads of light racing along them, brighter, a violet tint): the
      // satellites and dashed arc it had were a drawn graphic spinning, which didn't mesh with the rest.

      // Motes of light: round sparks each on its own slow orbit, drifting out from her and fading as they go.
      // They drift with uFlow (integrated by OrbMotion): out of her as she talks, into her while she listens. A light
      // feeling lifts them as they go and a heavy one lets them sink; a lively one makes them quicker and brighter.
      float moteLevel = (0.35 + 0.5 * uGlow + 0.5 * uKick + 0.3 * uEnergy) * (1.0 - 0.5 * uDrowse) * orbit * edge;
      if (moteLevel > 0.001 && r > 0.75 && r < 3.0 + 1.5 * (1.0 - uForm)) {
        float motes = 0.0;
        for (int k = 0; k < ${MOTES}; k++) {
          float fk = float(k);
          float h1 = hash(vec2(fk, 1.7));
          float h2 = hash(vec2(fk, 9.1));
          float h3 = hash(vec2(fk, 4.3));
          // 0 born at her edge .. 1 gone; each one's rate a whole number of 32nds of uFlow (FLOW_WRAP).
          float life = fract(h3 + uFlow * (3.0 + floor(h2 * 4.0)) / 32.0);
          float a = h1 * TAU + uTime * (0.12 + 0.2 * h2) * (h3 > 0.5 ? 1.0 : -1.0);
          float rad = (1.08 + 1.0 * life + 1.4 * (1.0 - uForm) * h2) * uRadius;
          vec2 at = vec2(cos(a), sin(a)) * rad + vec2(0.0, -uLift * life * life * 0.7 * uRadius);
          float dpx = length(q - at);
          float size = (0.8 + 1.2 * h2) * (1.0 + 0.3 * uEnergy);
          float fade = smoothstep(0.0, 0.15, life) * (1.0 - smoothstep(0.55, 1.0, life));
          // A four-pointed twinkle, not a dot.
          vec2 o = abs(q - at);
          float star = exp(-dpx * dpx / (size * size))
            + 0.5 * (exp(-o.x / (0.35 * size)) * exp(-o.y / (3.0 * size)) + exp(-o.y / (0.35 * size)) * exp(-o.x / (3.0 * size)));
          motes += star * fade * (0.75 + 0.25 * sin(uTime * (0.8 + 1.2 * h1) + h3 * 30.0));
        }
        light += hot * motes * moteLevel;
      }
    }
    light *= uAlpha;
    core *= uAlpha;
  }

  // ---- her flight's trail: a ribbon of light along where she's just been, thinning and fading as it ages, with a
  // few sparks shed along it ----
  if (uTrail[0].z > 0.0 && uAlpha > 0.001) {
    float trail = 0.0;
    vec2 prev = uCentre;
    float prevAge = 0.0;
    for (int i = 0; i < ${TRAIL_POINTS}; i++) {
      vec3 tp = uTrail[i];
      if (tp.z <= 0.0) break;
      float age = tp.z;
      vec2 ab = tp.xy - prev;
      float t = clamp(dot(p - prev, ab) / max(dot(ab, ab), 1e-4), 0.0, 1.0);
      float k = mix(prevAge, age, t); // 0 new .. 1 gone
      float dd = length(p - prev - ab * t);
      float w = max(1.3, uRadius * 0.55 * (1.0 - k));
      float fade = (1.0 - k) * (1.0 - k);
      // Sparks: noise fixed on the screen, so they stay where she dropped them and fade there.
      float sp = smoothstep(0.72, 0.9, noise(p * 0.35 + 17.0)) * exp(-dd * dd / (w * w * 4.0));
      // (A soft halo round the ribbon, not an exponential tail: that spread a faint glow over the whole window
      // the moment she set off.)
      trail = max(trail, (exp(-dd * dd / (w * w)) * 0.55 + exp(-dd * dd / (w * w * 6.0)) * 0.2 + sp * 0.8) * fade);
      prev = tp.xy;
      prevAge = age;
    }
    // Faded in from her middle out, as the comet tail is, so it never draws over her face.
    float off = smoothstep(uRadius * 0.6, uRadius * 1.4, length(p - uCentre));
    vec2 win = uRes / uDpr;
    off *= smoothstep(0.0, 12.0, min(min(p.x, p.y), min(win.x - p.x, win.y - p.y))); // never cut off square
    light += mix(col, white, 0.25) * trail * off * uAlpha;
  }

  // ---- pointing: the pointer's tail to its tip, and the ring round the thing ----
  if (uMorph > 0.001 && uRadius > 0.1) {
    float d = taper(p, uCentre, uTip, uRadius * 0.9, 1.2);
    float shape = 1.0 - smoothstep(-0.8, 0.8, d);
    float edgeGlow = exp(-max(d, 0.0) * 0.25) * 0.6;
    // Light running down it toward the tip, in soft pulses: what she's showing is where it flows.
    vec2 ct = uTip - uCentre;
    float down = clamp(dot(p - uCentre, ct) / max(dot(ct, ct), 1e-4), 0.0, 1.0);
    float pulse = pow(0.5 + 0.5 * sin(down * 9.0 - uTime * 5.0), 6.0) * (0.3 + 0.7 * down);
    // Only once she's mostly gathered in: drawn over the big orb, the pointer read as a grey film.
    float late = smoothstep(0.5, 1.0, uMorph) * (1.0 - core * (1.0 - uMorph));
    light += (mix(col, white, 0.65) * shape * (0.75 + 0.35 * pulse) + col * edgeGlow * (1.0 + 0.5 * pulse)) * late * uAlpha;
  }
  if (uRingAlpha > 0.001) {
    vec2 extent = uRing.zw * 0.5;
    vec2 c = uRing.xy + extent;
    float shrink = 1.0 - 0.35 * uRingDraw;
    vec2 rp = (p - c) / shrink;
    float d = roundRect(rp, extent, min(uRingRound, min(extent.x, extent.y)));
    float width = 1.6 + 2.5 * uRingFlash;
    float line = 1.0 - smoothstep(width - 0.8, width + 0.8, abs(d));
    float glow = exp(-abs(d) * (0.22 - 0.1 * uRingFlash));
    // Light in runs round it, from noise sampled round its middle in a frame drifting at its own pace (a lit
    // stretch carried rigidly round read as a picture turning, the user's complaint about the old rings).
    vec2 rdir = rp / max(length(rp), 1e-4);
    float psi = uTime * 0.6;
    vec2 rf = mat2(cos(psi), -sin(psi), sin(psi), cos(psi)) * rdir;
    float run = 0.5 + 0.5 * smoothstep(0.3, 0.8, noise(rf * 1.6 + vec2(3.7, uTime * 0.4)));
    float breathe = 1.0 + 0.12 * sin(uTime * 2.2);
    light += (white * line * (0.65 + 0.35 * run) + col * glow * (0.55 + 0.6 * uRingFlash) * run * breathe) * uRingAlpha;
    // The thing itself, faintly lit.
    light += col * (1.0 - smoothstep(-1.0, 1.0, d)) * 0.05 * uRingAlpha;
  }

  // (The scanlines and dither come after the blend with the frame before, in PRESENT: blended, dithered pixels
  // would smear into noise.)
  light = light / (1.0 + light * 0.35); // soft highlight roll-off rather than a hard clip
  // Hazy and a little desaturated, as the user's PS2 reference is (and her panels): a share of every colour goes to
  // a grey-blue of its own brightness, more when she's low.
  float lum = dot(light, vec3(0.25, 0.55, 0.2));
  light = mix(light, lum * vec3(0.7, 0.86, 1.08), 0.2 + 0.15 * max(-uLift, 0.0));
  float a = clamp(max(core * 0.94, max(light.r, max(light.g, light.b))), 0.0, 1.0);
  gl_FragColor = vec4(min(light, vec3(a)), a);
}
`;

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(`orb shader: ${gl.getShaderInfoLog(shader)}`);
  return shader;
}

function makeProgram(gl, fragment) {
  const program = gl.createProgram();
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX));
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, fragment));
  gl.bindAttribLocation(program, 0, 'aPos'); // one triangle buffer for all three passes
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(`orb program: ${gl.getProgramInfoLog(program)}`);
  return program;
}

const NAMES = [
  'uRes', 'uDpr', 'uTime', 'uCentre', 'uRadius', 'uAlpha', 'uStretch', 'uGlow', 'uSwirl', 'uShimmer', 'uSpin', 'uHalo',
  'uOrbit', 'uHue', 'uVoice', 'uKick', 'uLook', 'uMorph', 'uRipples', 'uRing', 'uRingRound', 'uRingAlpha', 'uRingFlash',
  'uRingDraw', 'uTip', 'uFlow', 'uSurface', 'uLift', 'uEnergy', 'uTrail', 'uForm', 'uDrowse', 'uGather', 'uGhost',
];

class OrbPainter {
  constructor(canvas) {
    this.canvas = canvas;
    this.lost = false;
    this.init();
  }

  // (Again after a lost context comes back: a GPU reset, a driver update.)
  init() {
    const { canvas } = this;
    const options = { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false, powerPreference: 'low-power' };
    const gl = canvas.getContext('webgl2', options) || canvas.getContext('webgl', options);
    if (!gl) throw new Error('WebGL is not available');
    this.gl = gl;
    this.program = makeProgram(gl, FRAGMENT);
    this.blend = makeProgram(gl, BLEND);
    this.present = makeProgram(gl, PRESENT);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.u = {};
    for (const name of NAMES) this.u[name] = gl.getUniformLocation(this.program, name === 'uRipples' || name === 'uTrail' ? `${name}[0]` : name);
    const at = (program, names) => Object.fromEntries(names.map((n) => [n, gl.getUniformLocation(program, n)]));
    this.ub = at(this.blend, ['uNow', 'uThen', 'uRes', 'uKeep']);
    this.up = at(this.present, ['uImage', 'uRes', 'uDpr', 'uTime']);
    gl.useProgram(this.blend);
    gl.uniform1i(this.ub.uNow, 0);
    gl.uniform1i(this.ub.uThen, 1);
    gl.useProgram(this.present);
    gl.uniform1i(this.up.uImage, 0);
    this.ripples = new Float32Array(2 * MAX_RIPPLES);
    this.trail = new Float32Array(3 * TRAIL_POINTS);
    this.targets = null; // {w, h, scene, history: [a, b]}: textures with their framebuffers, made at her size
    this.dpr = 1;
  }

  // A texture the size of the canvas, and a framebuffer drawing into it.
  target(w, h) {
    const { gl } = this;
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.NEAREST], [gl.TEXTURE_MAG_FILTER, gl.NEAREST], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) {
      gl.texParameteri(gl.TEXTURE_2D, k, v);
    }
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return { texture, fb };
  }

  // Forget the frames before: she's somewhere new (the window moved, she came back), so no ghost of where she was.
  forget() {
    if (this.targets) this.targets.fresh = true;
  }

  fit() {
    this.dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(window.innerWidth * this.dpr));
    const h = Math.max(1, Math.round(window.innerHeight * this.dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    const t = this.targets;
    if (!t || t.w !== w || t.h !== h) {
      const { gl } = this;
      if (t) for (const x of [t.scene, ...t.history]) { gl.deleteTexture(x.texture); gl.deleteFramebuffer(x.fb); }
      this.targets = { w, h, scene: this.target(w, h), history: [this.target(w, h), this.target(w, h)], fresh: true };
    }
    this.gl.viewport(0, 0, w, h);
  }

  clear() {
    if (this.lost) return;
    this.fit();
    const { gl } = this;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    this.forget();
  }

  // Her frame, `dt` s after the last one: her scene into a texture, blended with the frame before, then on screen
  // with the scanlines and dither.
  draw(m, dt) {
    if (this.lost) return;
    this.fit();
    const { gl, u } = this;
    if (!m.visible) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      this.forget();
      return;
    }
    const t = this.targets;
    gl.bindFramebuffer(gl.FRAMEBUFFER, t.scene.fb);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program);
    const at = m.position();
    const look = m.look;
    const stretch = m.stretch;
    gl.uniform2f(u.uRes, this.canvas.width, this.canvas.height);
    gl.uniform1f(u.uDpr, this.dpr);
    gl.uniform1f(u.uTime, m.time); // OrbMotion wraps it (TIME_WRAP_S)
    gl.uniform2f(u.uCentre, at.x, at.y);
    gl.uniform1f(u.uRadius, m.radius);
    gl.uniform1f(u.uAlpha, m.alpha);
    gl.uniform3f(u.uStretch, stretch.x, stretch.y, stretch.amount);
    gl.uniform1f(u.uGlow, look.glow);
    gl.uniform1f(u.uSwirl, look.swirl);
    gl.uniform1f(u.uShimmer, look.shimmer);
    gl.uniform1f(u.uSpin, m.spinPhase);
    gl.uniform1f(u.uHalo, look.halo);
    gl.uniform1f(u.uOrbit, look.orbit);
    gl.uniform1f(u.uHue, look.hue);
    gl.uniform1f(u.uVoice, m.voiceGlow); // her voice as a steady glow, not a pump a syllable
    gl.uniform1f(u.uKick, m.kick);
    gl.uniform2f(u.uLook, m.gaze.x, m.gaze.y);
    gl.uniform1f(u.uMorph, m.morph);
    this.ripples.fill(0);
    m.ripples.forEach((r, i) => {
      this.ripples[2 * i] = r.age;
      this.ripples[2 * i + 1] = r.strength;
    });
    gl.uniform2fv(u.uRipples, this.ripples);
    gl.uniform1f(u.uFlow, m.flow);
    gl.uniform1f(u.uSurface, m.surface);
    gl.uniform1f(u.uLift, m.feel.lift);
    gl.uniform1f(u.uEnergy, m.feel.energy);
    gl.uniform1f(u.uForm, m.form);
    gl.uniform1f(u.uDrowse, m.drowse);
    gl.uniform1f(u.uGather, m.gathered);
    const g = m.ghost; // (fields, not ghostUniform: no array a frame)
    if (g) gl.uniform4f(u.uGhost, g.age / GHOST_S, g.kind, g.sides, g.seed);
    else gl.uniform4f(u.uGhost, -1, 0, 0, 0);
    // Her path's points with their age (0 new .. 1 gone; 0 itself marks the end of the list).
    this.trail.fill(0);
    m.trail.forEach((t, i) => {
      this.trail[3 * i] = t.x;
      this.trail[3 * i + 1] = t.y;
      this.trail[3 * i + 2] = Math.max(1e-3, t.age / TRAIL_S);
    });
    gl.uniform3fv(u.uTrail, this.trail);
    const h = m.highlight;
    if (h) {
      gl.uniform4f(u.uRing, h.ring.x, h.ring.y, h.ring.w, h.ring.h);
      gl.uniform1f(u.uRingRound, h.ring.round);
      gl.uniform1f(u.uRingAlpha, h.alpha);
      gl.uniform1f(u.uRingFlash, h.flash);
      gl.uniform1f(u.uRingDraw, h.drawIn);
      gl.uniform2f(u.uTip, h.tip.x, h.tip.y);
    } else {
      gl.uniform1f(u.uRingAlpha, 0);
      const tip = m.pointing ? m.pointing.geometry.tip : at;
      gl.uniform2f(u.uTip, tip.x, tip.y);
    }
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    // The blend: the brighter of this frame and the last one faded.
    const [then, now] = t.history;
    gl.bindFramebuffer(gl.FRAMEBUFFER, now.fb);
    gl.useProgram(this.blend);
    gl.uniform2f(this.ub.uRes, t.w, t.h);
    gl.uniform1f(this.ub.uKeep, t.fresh || !(dt > 0) ? 0 : Math.exp(-dt / BLEND_TAU));
    t.fresh = false;
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, then.texture);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, t.scene.texture);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    t.history = [now, then];

    // The screen.
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.present);
    gl.uniform2f(this.up.uRes, t.w, t.h);
    gl.uniform1f(this.up.uDpr, this.dpr);
    gl.uniform1f(this.up.uTime, m.time);
    gl.bindTexture(gl.TEXTURE_2D, now.texture);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}

const finite = (...values) => values.every(Number.isFinite);
const toPoint = (p) => (p && finite(p.x, p.y) ? { x: p.x, y: p.y } : null);
const toRect = (r) => (r && finite(r.x, r.y, r.w, r.h) && r.w > 0 && r.h > 0 ? { x: r.x, y: r.y, w: r.w, h: r.h } : null);

export async function runOrb(host, cfg, canvas) {
  document.title = 'Octavia';
  canvas.style.opacity = '1'; // her own alpha does the fading in and out
  const painter = new OrbPainter(canvas);
  // A GPU reset loses the context: wait for it to come back and build the shader again (three.js does the same for
  // the character), rather than staying invisible for good.
  canvas.addEventListener('webglcontextlost', (e) => {
    e.preventDefault();
    painter.lost = true;
    console.error('orb: the WebGL context was lost; waiting for it to come back');
  });
  canvas.addEventListener('webglcontextrestored', () => {
    try {
      painter.init();
      painter.lost = false;
    } catch (err) {
      host.emit({ type: 'error', message: `orb: the WebGL context came back but ${err.message}` });
    }
  });
  const motion = new OrbMotion();
  motion.layout({ w: window.innerWidth, h: window.innerHeight }, null);

  let running = false;
  let last = 0;
  let gone = null; // {frames, messages}: out of sight; report it once a blank frame is up
  let pendingPoint = null;
  let atHome = true;
  // Debug, for tools/drive.mjs's clips: as in app.js, with the clock held only 'tick' messages move her on.
  let clockHeld = false;

  const goneAfterFrame = (msg) => {
    if (!running) host.emit(msg);
    else if (gone) gone.messages.push(msg);
    else gone = { frames: 2, messages: [msg] };
  };
  const beginPointing = ({ target, box }) => {
    pendingPoint = null;
    motion.point(target, box);
  };
  const onEvent = (name) => {
    if (name === 'arrived') {
      host.emit({ type: 'arrived' });
      if (pendingPoint) beginPointing(pendingPoint);
    } else if (name === 'zipped-out') {
      pendingPoint = null;
      goneAfterFrame({ type: 'zipped-out' });
    } else if (name === 'hidden') {
      goneAfterFrame({ type: 'hidden' });
    }
  };
  // From her spot toward a window px point, or null.
  const directionTo = (p) => {
    if (!p) return null;
    const r = motion.rest;
    return { x: p.x - r.x, y: p.y - r.y };
  };

  host.onLayout?.((l) => {
    if (!l || typeof l !== 'object') return;
    atHome = l.mode !== 'point' && l.mode !== 'perch';
    painter.forget(); // the window moved: no ghost of where she was in it
    const size = { w: window.innerWidth, h: window.innerHeight };
    if (finite(l.width, l.height) && l.width > 0 && l.height > 0) Object.assign(size, { w: l.width, h: l.height });
    motion.layout(size, atHome ? null : toRect(l.body));
  });

  // ---- pointer: hit test, see-through, drag ----
  const pointer = { x: -1, y: -1, inside: false };
  let over = false;
  let dragging = false;
  const seeThrough = new SeeThrough(host, (on) => canvas.classList.toggle('see-through', on));
  const setOver = (value) => {
    if (value === over) return;
    over = value;
    seeThrough.setOver(value);
  };
  canvas.addEventListener('pointermove', (e) => {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.inside = true;
  });
  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || !over || !seeThrough.takesMouse) return;
    dragging = true;
    seeThrough.setDragging(true);
    canvas.setPointerCapture(e.pointerId);
    host.drag('start');
  });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    host.drag('end');
    seeThrough.setDragging(false);
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('lostpointercapture', endDrag);
  host.onCursor((p) => {
    motion.lookAt(p.x, p.y);
    if (dragging) return;
    pointer.inside = p.inside;
    if (p.inside) {
      pointer.x = p.x;
      pointer.y = p.y;
    }
  });

  // Debug, for tools/drive.mjs's orb-live: the gaps between her frames and the main-thread time each took (ms).
  const stats = { gaps: [], work: [] };
  const summary = (values) => {
    const sorted = [...values].sort((a, b) => a - b);
    const at = (q) => (sorted.length ? +sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))].toFixed(2) : null);
    const mean = sorted.length ? +(sorted.reduce((a, b) => a + b, 0) / sorted.length).toFixed(2) : null;
    return { mean, p95: at(0.95), max: at(1) };
  };

  // ---- frame loop (only while she's on screen) ----
  let rafMs = TARGET_MS;
  let targetMs = TARGET_MS; // debug 'pace' changes it, for measuring what a frame rate costs
  let prevRaf = null;
  let skipped = 0;
  const frame = (now) => {
    if (!running) return;
    // Every frame of the display while she's on screen, idle too: unlike the character she's one cheap shader pass,
    // and at half rate her sparks and swirl stepped visibly (it read as laggy).
    if (prevRaf != null) rafMs += 0.1 * (Math.min(100, now - prevRaf) - rafMs); // the display's frame time, smoothed
    prevRaf = now;
    const stride = Math.max(1, Math.round(targetMs / Math.max(1, rafMs)));
    if (clockHeld || ++skipped < stride) {
      requestAnimationFrame(frame);
      return;
    }
    skipped = 0;
    const dt = Math.min(MAX_DT, Math.max(0, (now - last) / 1000));
    stats.gaps.push(now - last);
    last = now;
    const began = performance.now();
    motion.update(dt);
    motion.takeEvents().forEach(onEvent);
    painter.draw(motion, dt);
    stats.work.push(performance.now() - began);
    if (!dragging && motion.visible) setOver(seeThrough.follow(pointer.inside && motion.contains(pointer.x, pointer.y), pointer.inside, now));
    if (gone && gone.frames-- === 0) {
      const { messages } = gone;
      gone = null;
      running = false;
      setOver(false);
      messages.forEach((msg) => host.emit(msg));
      return;
    }
    if (!motion.visible && !gone) {
      // Out of sight with nothing left to report (a hide while already gone): no more frames until she's back.
      running = false;
      setOver(false);
      return;
    }
    requestAnimationFrame(frame);
  };
  const start = () => {
    if (running) return;
    running = true;
    prevRaf = null;
    skipped = Infinity; // the first frame is drawn at once
    last = performance.now();
    requestAnimationFrame(frame);
  };
  // Events that came from a call rather than a frame (hiding while already out of sight).
  const flush = () => motion.takeEvents().forEach(onEvent);

  const handlers = {
    show(msg) {
      gone = null;
      const from = msg.reason === 'return' ? toPoint(msg.from) : null;
      if (from && motion.settled) host.emit({ type: 'arrived' });
      else if (!from || !motion.zipIn(directionTo(from))) motion.show({ arrive: msg.reason === 'return' });
      flush();
      start();
    },
    hide() {
      pendingPoint = null;
      motion.endWork();
      motion.unpoint();
      motion.hide();
      flush();
      if (motion.visible) start();
    },
    point(msg) {
      const target = toPoint(msg.target);
      if (!target) return;
      motion.endWork(); // pointing ends any activity: the bridge doesn't say so
      const request = { target, box: toRect(msg.box) };
      gone = null;
      if (motion.settled) {
        beginPointing(request);
        host.emit({ type: 'arrived' });
        start();
        return;
      }
      pendingPoint = request;
      motion.zipIn(directionTo(toPoint(msg.from)));
      start();
    },
    unpoint() {
      pendingPoint = null;
      motion.unpoint();
    },
    'point-ending'(msg) {
      motion.countdown(Number(msg.seconds));
    },
    press() {
      motion.press();
    },
    'zip-out'(msg) {
      pendingPoint = null;
      if (motion.zipOut(directionTo(toPoint(msg.to)))) {
        start();
        return;
      }
      motion.pointing = null;
      host.emit({ type: 'zipped-out' });
    },
    keys(msg) {
      seeThrough.setCtrl(msg.ctrl === true);
    },
    glance(msg) {
      if (motion.settled) motion.lookAt(Number(msg.x), Number(msg.y), true);
    },
    startle() {
      if (motion.settled) motion.spark(1);
    },
    state(msg) {
      if (typeof msg.state === 'string') motion.setState(msg.state);
    },
    working(msg) {
      motion.working = msg.on === true;
    },
    activity(msg) {
      // A start that crossed a hide is dropped, as the character drops it.
      motion.activity = typeof msg.name === 'string' && motion.visible;
    },
    snapshot() {
      if (!motion.visible) return;
      motion.spark(0.9);
      motion.briefWork();
      host.emit({ type: 'shutter' }); // Octavia's shutter sound lands on this flash
    },
    moment(msg) {
      if (!motion.visible) return;
      motion.spark(msg.name === 'rainboom' ? 1 : 0.6);
      motion.briefWork();
      if (msg.name === 'snapshot') host.emit({ type: 'shutter' });
      if (msg.name === 'rainboom') {
        const at = motion.position();
        host.emit({ type: 'rainboom', x: Math.round(at.x), y: Math.round(at.y) });
      }
    },
    segment(msg) {
      motion.spark(0.2 * (Number(msg.intensity) || 0)); // a little swell a sentence: more drew the eye every few seconds
      motion.feelFor(msg.emotion, Number(msg.intensity), Number(msg.hold) || 4);
    },
    emotion(msg) {
      motion.spark(0.5 * (Number(msg.intensity) || 0));
      motion.feelFor(msg.emotion, Number(msg.intensity), Number(msg.hold));
    },
    // A feeling that carries over after the turn (thanked, she stays a little happy).
    mood(msg) {
      motion.feelFor(msg.emotion, Number(msg.intensity), Number(msg.seconds));
    },
    mouth(msg) {
      motion.setVoice(Number(msg.level));
    },
    gesture(msg) {
      if (msg.name === 'impatient') motion.briefWork();
      else motion.spark(0.3);
    },
    // Debug, for tools/drive.mjs: a layout or a held pointer, as in app.js; other renderer messages pass through.
    debug(msg) {
      const inner = msg.message;
      if (!inner || typeof inner.type !== 'string' || inner.type === 'debug') return;
      if (inner.type === 'pace') {
        if (Number(inner.fps) > 0) targetMs = 1000 / Number(inner.fps);
      } else if (inner.type === 'stats') {
        console.log(`orb stats ${JSON.stringify({ frames: stats.gaps.length, gapMs: summary(stats.gaps), workMs: summary(stats.work) })}`);
        stats.gaps.length = 0;
        stats.work.length = 0;
      } else if (inner.type === 'clock') {
        clockHeld = inner.held === true;
        last = performance.now();
      } else if (inner.type === 'tick') {
        // `frames` steps of `dt` s, then her frame; once it's on screen, "ticked <id>" for the capture that follows.
        const dt = Math.min(MAX_DT, Math.max(0, Number(inner.dt) || 0));
        const frames = Math.min(600, Math.max(1, Math.round(Number(inner.frames) || 1)));
        for (let i = 0; i < frames; i++) {
          motion.update(dt);
          motion.takeEvents().forEach(onEvent);
        }
        painter.draw(motion, dt * frames);
        requestAnimationFrame(() => requestAnimationFrame(() => console.log(`ticked ${inner.id ?? ''}`)));
      } else if (inner.type === 'layout') {
        painter.forget();
        motion.layout({ w: inner.width || window.innerWidth, h: inner.height || window.innerHeight }, inner.mode === 'point' || inner.mode === 'perch' ? toRect(inner.body) : null);
      } else if (inner.type === 'ghost') {
        // {off: true}: none at all (for clips compared before and after); else one now.
        if (inner.off === true) {
          motion.ghost = null;
          motion.untilGhost = Infinity;
        } else motion.summonGhost(Number.isInteger(inner.kind) ? inner.kind : null);
      } else if (inner.type === 'hover') {
        const p = toPoint(inner);
        pointer.inside = !!p;
        if (p) Object.assign(pointer, p);
      } else if (Object.hasOwn(handlers, inner.type)) handlers[inner.type](inner);
    },
  };
  host.onMessage((msg) => {
    const handler = msg && Object.hasOwn(handlers, msg.type) ? handlers[msg.type] : null;
    if (!handler) return;
    try {
      handler(msg);
    } catch (err) {
      console.error(`message ${msg.type} failed: ${err.stack || err}`);
    }
  });

  // Draw one frame now, so the shader is compiled before anyone sees her.
  painter.clear();
  host.emit({ ...ORB_METRICS });
  host.emit({ type: 'ready' });
}
