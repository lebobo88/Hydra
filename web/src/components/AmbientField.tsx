/**
 * AmbientField — living ambient particle field behind the Working area.
 *
 * Renders drifting ember / tongue-of-fire particles rising toward the Spirit,
 * with subtle parallax depth, slow scale-texture drift via CSS, and a
 * vignette breathing effect.
 *
 * Technical discipline:
 *  - Canvas, NOT DOM particles (avoids layout thrash)
 *  - DPR capped at 2 (prevents 4K overdraw)
 *  - RAF loop pauses when document.hidden (Page Visibility API)
 *  - prefers-reduced-motion: canvas hidden; only static CSS vignette shown
 *  - Max 40 particles; particle count zero during tab-hidden
 *  - GPU-friendly: all compositing on the canvas, no layout-affecting props
 *  - aria-hidden="true" — decorative layer; zero AT impact
 */

import { useEffect, useRef } from 'react';

// ---------------------------------------------------------------------------
// Particle type
// ---------------------------------------------------------------------------

interface Ember {
  x: number;   // canvas px
  y: number;   // canvas px (starts near bottom, drifts upward)
  vy: number;  // upward velocity (negative = up)
  vx: number;  // horizontal drift
  size: number;// radius in px
  alpha: number; // current opacity
  alphaDelta: number; // per-frame fade change (positive = fading in, negative = fading out)
  hue: number; // 28–42 (amber → gold range)
  life: number; // 0–1 lifecycle position
  phase: number; // horizontal sway phase offset
}

// ---------------------------------------------------------------------------
// Constants — tuned for visible but ambient presence
// ---------------------------------------------------------------------------

const MAX_PARTICLES = 40;
const SPAWN_RATE = 0.35;     // particles spawned per frame on average
const MAX_DPR = 2;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function rand(min: number, max: number): number {
  return min + Math.random() * (max - min);
}

function spawnEmber(w: number, h: number): Ember {
  return {
    x: rand(0, w),
    y: rand(h * 0.5, h),          // spawn in bottom half
    vy: rand(-0.4, -1.4),         // drift upward
    vx: rand(-0.15, 0.15),        // gentle horizontal wander
    size: rand(1.2, 3.5),
    alpha: 0,
    alphaDelta: rand(0.006, 0.014), // fade-in rate
    hue: rand(28, 48),
    life: 0,
    phase: rand(0, Math.PI * 2),
  };
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface AmbientFieldProps {
  /** CSS class to apply to the wrapper div */
  className?: string;
}

export function AmbientField({ className = '' }: AmbientFieldProps): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frameRef = useRef<number>(0);
  const particlesRef = useRef<Ember[]>([]);
  const timeRef = useRef<number>(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    // Respect prefers-reduced-motion — bail out early (canvas stays hidden via CSS)
    // Guard against environments without matchMedia (SSR / jsdom)
    let reduceMotion = false;
    try {
      if (typeof window.matchMedia === 'function') {
        const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
        reduceMotion = mq != null && mq.matches === true;
      }
    } catch {
      // matchMedia unavailable — assume motion allowed
    }
    if (reduceMotion) return;

    // Guard: RAF must be available (not present in all test environments)
    if (typeof requestAnimationFrame !== 'function') return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // ---- Sizing ----
    const dpr = Math.min(window.devicePixelRatio ?? 1, MAX_DPR);

    function resize(): void {
      if (!canvas) return;
      const parent = canvas.parentElement;
      if (!parent) return;
      const w = parent.clientWidth;
      const h = parent.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    resize();
    const ro = new ResizeObserver(resize);
    if (canvas.parentElement) ro.observe(canvas.parentElement);

    // ---- Visibility gate ----
    let hidden = document.hidden;
    function onVisibilityChange(): void {
      hidden = document.hidden;
      if (!hidden && frameRef.current === 0) {
        frameRef.current = requestAnimationFrame(tick);
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange);

    // ---- Animation loop ----
    function tick(ts: number): void {
      if (hidden) {
        frameRef.current = 0;
        return;
      }

      if (!canvas || !ctx) return;

      const dt = Math.min(ts - (timeRef.current || ts), 50); // clamp frame delta to 50ms
      timeRef.current = ts;
      const w = canvas.width / dpr;
      const h = canvas.height / dpr;

      // Clear
      ctx.clearRect(0, 0, w, h);

      // Spawn new particles
      if (particlesRef.current.length < MAX_PARTICLES && Math.random() < SPAWN_RATE) {
        particlesRef.current.push(spawnEmber(w, h));
      }

      // Update + draw
      const dtFactor = dt / 16.67; // normalize to 60fps
      particlesRef.current = particlesRef.current.filter((p) => {
        // ---- Update ----
        p.life += 0.004 * dtFactor;
        p.y += p.vy * dtFactor;
        p.x += p.vx * dtFactor + Math.sin(p.phase + p.life * 4) * 0.3 * dtFactor;
        p.phase += 0.015 * dtFactor;

        // Fade in then fade out
        if (p.alpha < 0.85 && p.life < 0.5) {
          p.alpha = Math.min(0.85, p.alpha + p.alphaDelta * dtFactor);
        } else if (p.life >= 0.5) {
          p.alpha = Math.max(0, p.alpha - p.alphaDelta * 0.7 * dtFactor);
        }

        // Slight size shrink as it rises (taper like a flame tongue)
        const currentSize = p.size * (1 - p.life * 0.4);

        // Remove dead particles (off-screen top or fully faded or max life)
        if (p.y < -20 || p.alpha <= 0 || p.life > 1) return false;

        // ---- Draw ----
        if (!ctx) return false;

        // Outer glow halo
        const gradient = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, currentSize * 3.5);
        gradient.addColorStop(0, `hsla(${p.hue}, 90%, 68%, ${p.alpha * 0.9})`);
        gradient.addColorStop(0.4, `hsla(${p.hue}, 85%, 55%, ${p.alpha * 0.5})`);
        gradient.addColorStop(1, `hsla(${p.hue}, 80%, 45%, 0)`);

        ctx.beginPath();
        ctx.arc(p.x, p.y, currentSize * 3.5, 0, Math.PI * 2);
        ctx.fillStyle = gradient;
        ctx.fill();

        // Inner bright core
        const coreGrad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, currentSize);
        coreGrad.addColorStop(0, `hsla(52, 100%, 88%, ${p.alpha})`);
        coreGrad.addColorStop(1, `hsla(${p.hue}, 95%, 65%, ${p.alpha * 0.7})`);

        ctx.beginPath();
        ctx.arc(p.x, p.y, currentSize, 0, Math.PI * 2);
        ctx.fillStyle = coreGrad;
        ctx.fill();

        return true;
      });

      frameRef.current = requestAnimationFrame(tick);
    }

    frameRef.current = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = 0;
      document.removeEventListener('visibilitychange', onVisibilityChange);
      ro.disconnect();
    };
  }, []);

  return (
    <div
      className={`ambient-field-wrapper${className ? ` ${className}` : ''}`}
      aria-hidden="true"
      data-testid="ambient-field"
    >
      {/* Canvas — only shown when motion allowed (CSS hides it under prefers-reduced-motion) */}
      <canvas
        ref={canvasRef}
        className="ambient-field-canvas"
        aria-hidden="true"
      />
      {/* Static vignette — always present, including reduced-motion */}
      <div className="ambient-vignette" aria-hidden="true" />
      {/* Scale-texture drift layer — CSS-only, parallax depth */}
      <div className="ambient-scale-drift" aria-hidden="true" />
    </div>
  );
}
