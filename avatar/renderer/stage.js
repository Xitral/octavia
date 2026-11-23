// The three.js side: renderer, lights, camera framing and the alpha hit test for click-through.
//
// The camera frames her for a "framing viewport": the rectangle `body` inside the window where her
// framing (whole body or waist up) sits. At home that is the whole window. When she points at something
// the window grows around her and the target, and the camera's view offset turns the extra window area
// into more of the same image plane, so she looks exactly the same wherever the body rectangle sits, with
// no perspective skew, and project/unproject stay consistent across the whole window.

import * as THREE from 'three';

const FOV = 22; // a longish lens keeps the body from looking distorted at desktop-widget sizes
// A window can be much bigger than her while she points; past this many device pixels the drawing
// buffer drops below the display's pixel ratio rather than eating GPU memory and fill rate.
const MAX_BUFFER_PIXELS = 2048 * 2048;

export class Stage {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, premultipliedAlpha: true });
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.gl = this.renderer.getContext();
    this.pixel = new Uint8Array(4);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 100);
    this.framing = 'full';
    this.body = null; // framing viewport in window CSS px; null = the whole window
    this.width = 1;
    this.height = 1;

    // MToon shades with one main light (lit vs. shade color); the hemisphere light lifts the shade side
    // so the toon shadows stay soft instead of going muddy.
    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.position.set(-0.6, 1.4, 2.0);
    this.scene.add(key);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xb8b0c8, 0.7));
    this.resize();
    this.watchPixelRatio();
    // The main process resizes the window when she flies somewhere; the canvas follows it through CSS.
    new ResizeObserver(() => this.resize()).observe(canvas);
  }

  // Dragging her to a monitor with different scaling changes the pixel ratio without a resize event.
  watchPixelRatio() {
    matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener(
      'change',
      () => {
        this.resize();
        this.watchPixelRatio();
      },
      { once: true },
    );
  }

  resize() {
    const w = this.canvas.clientWidth || window.innerWidth;
    const h = this.canvas.clientHeight || window.innerHeight;
    const ratio = Math.min(window.devicePixelRatio, 2, Math.sqrt(MAX_BUFFER_PIXELS / (w * h)));
    this.renderer.setPixelRatio(ratio);
    this.renderer.setSize(w, h, false);
    this.width = w;
    this.height = h;
    this.updateView();
  }

  // Remembers the model's rest bounds (feet at y = 0) and hips height, which every framing is built from.
  setModel(bounds, hipsY) {
    this.modelHeight = bounds.max.y;
    this.hipsY = hipsY;
    this.frame();
  }

  // The window layout from the main process: which framing, and where its viewport sits in the window.
  layout({ framing, body }) {
    this.framing = framing === 'upper' ? 'upper' : 'full';
    this.body = body || null;
    this.frame();
    this.updateView();
  }

  // What a framing shows at her depth, in metres: the whole body plus headroom for a raised hand, or from
  // the waist up with a little air above the hair.
  viewSpan(framing) {
    const height = this.modelHeight;
    const top = height * (framing === 'upper' ? 1.04 : 1.1);
    const bottom = framing === 'upper' ? this.hipsY * 0.92 : -height * 0.015;
    return { top, bottom, span: top - bottom };
  }

  frame() {
    if (this.modelHeight === undefined) return;
    const { top, bottom, span } = this.viewSpan(this.framing);
    const distance = span / 2 / Math.tan(THREE.MathUtils.degToRad(FOV / 2));
    const centerY = (top + bottom) / 2;
    // A level camera keeps verticals vertical; she stands at the origin facing +Z, toward us.
    this.camera.position.set(0, centerY, distance);
    this.camera.lookAt(0, centerY, 0);
    this.camera.updateMatrixWorld();
    this.span = span;
  }

  // The framing viewport is the camera's "full view" (its aspect is the viewport's); the window is a
  // sub-view of it that may reach past its edges.
  updateView() {
    const b = this.bodyRect;
    this.camera.setViewOffset(b.w, b.h, -b.x, -b.y, this.width, this.height);
  }

  get bodyRect() {
    return this.body || { x: 0, y: 0, w: this.width, h: this.height };
  }

  // Screen pixels per meter at her depth, for turning cursor offsets into distances.
  get pixelsPerMeter() {
    return this.bodyRect.h / this.span;
  }

  // World point -> CSS pixels in the window. Uses the size the projection was built for, so it always
  // agrees with rayAt even while a resize is on its way.
  toWindow(point, out = { x: 0, y: 0 }) {
    const p = _v.copy(point).project(this.camera);
    out.x = ((p.x + 1) / 2) * this.width;
    out.y = ((1 - p.y) / 2) * this.height;
    return out;
  }

  // The camera ray through window CSS pixel (x, y).
  rayAt(x, y, out = new THREE.Ray()) {
    const ndc = _v.set((x / this.width) * 2 - 1, 1 - (y / this.height) * 2, 0.5).unproject(this.camera);
    out.origin.copy(this.camera.position);
    out.direction.copy(ndc).sub(this.camera.position).normalize();
    return out;
  }

  render() {
    this.renderer.render(this.scene, this.camera);
  }

  // Alpha of the pixel under (x, y) CSS px in the frame just rendered. Must be called right after render():
  // the drawing buffer is not preserved past the frame.
  alphaAt(x, y) {
    const scale = this.canvas.width / this.canvas.clientWidth;
    const px = Math.floor(x * scale);
    const py = this.canvas.height - 1 - Math.floor(y * scale);
    if (px < 0 || py < 0 || px >= this.canvas.width || py >= this.canvas.height) return 0;
    this.gl.readPixels(px, py, 1, 1, this.gl.RGBA, this.gl.UNSIGNED_BYTE, this.pixel);
    return this.pixel[3];
  }
}

const _v = new THREE.Vector3();
