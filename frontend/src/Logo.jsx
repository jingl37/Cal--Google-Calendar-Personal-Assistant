import { useEffect, useRef } from "react";

function Logo ({ size = 96, active = false }) {
  const canvasRef = useRef(null);
  const rafRef = useRef(null);
  const rotRef = useRef({ x: 0.4, y: 0 });
  const pointsRef = useRef([]);

  useEffect(() => {
    // Fibonacci sphere sampling for even point distribution
    const COUNT = size > 200 ? 2600 : 900;
    const pts = [];
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < COUNT; i++) {
      const y = 1 - (i / (COUNT - 1)) * 2;
      const r = Math.sqrt(1 - y * y);
      const theta = golden * i;
      const x = Math.cos(theta) * r;
      const z = Math.sin(theta) * r;
      pts.push({ x, y, z, seed: Math.random() * Math.PI * 2 });
    }
    pointsRef.current = pts;
  }, [size]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    let t = 0;
    const speed = active ? 0.006 : 0.003;

    const render = () => {
      t += speed;
      rotRef.current.y += 0.0022;

      ctx.clearRect(0, 0, size, size);
      const cx = size / 2;
      const cy = size / 2;
      const baseR = size * 0.36;

      const { x: rx, y: ry } = rotRef.current;
      const cosY = Math.cos(ry), sinY = Math.sin(ry);
      const cosX = Math.cos(rx), sinX = Math.sin(rx);

      const projected = pointsRef.current.map((p) => {
        // layered "noise" displacement -> lumpy blob silhouette
        const n =
          Math.sin(p.x * 3.2 + t * 1.3 + p.seed) * 0.16 +
          Math.sin(p.y * 4.1 - t * 1.0 + p.seed) * 0.12 +
          Math.sin(p.z * 2.6 + t * 1.6) * 0.14;
        const disp = 1 + n;

        let x = p.x * disp, y = p.y * disp, z = p.z * disp;

        // rotate Y
        let x1 = x * cosY - z * sinY;
        let z1 = x * sinY + z * cosY;
        // rotate X
        let y1 = y * cosX - z1 * sinX;
        let z2 = y * sinX + z1 * cosX;

        const scale = 1.5 / (1.5 + z2);
        return {
          sx: cx + x1 * baseR * scale,
          sy: cy + y1 * baseR * scale,
          z: z2,
          n,
        };
      });

      projected.sort((a, b) => a.z - b.z);

      for (const p of projected) {
        const depth = (p.z + 1) / 2; // 0 back -> 1 front
        const glow = Math.max(0, p.n) * 2.2 + depth * 0.55;
        const dotR = 0.6 + depth * 1.3;

        // moss green core -> marigold rim
        const hue = 89 - glow * 43;
        const light = 39 + glow * 24 + depth * 12;
        const alpha = 0.35 + depth * 0.65;

        ctx.beginPath();
        ctx.fillStyle = `hsla(${hue}, 75%, ${Math.min(light, 72)}%, ${alpha})`;
        ctx.arc(p.sx, p.sy, dotR, 0, Math.PI * 2);
        ctx.fill();
      }

      rafRef.current = requestAnimationFrame(render);
    };

    render();
    return () => cancelAnimationFrame(rafRef.current);
  }, [size, active]);

  return (
    <canvas
      ref={canvasRef}
      style={{
        width: size,
        height: size,
        display: "block",
        filter: active ? "drop-shadow(0 0 24px rgba(154,172,99,0.48))" : "drop-shadow(0 0 12px rgba(154,172,99,0.28))",
        transition: "filter 0.4s ease",
      }}
    />
  );
}

export default Logo
