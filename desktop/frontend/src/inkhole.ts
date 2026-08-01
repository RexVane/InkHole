export type InkHoleVariant = "main" | "pet";

type ShardAnimation = {
    kind: "absorb" | "emit";
    startedAt: number;
};

// 与旧版 inkhole.qml / Python InkHoleHero 使用同一组形态参数。
export class InkHoleAnimation {
    private readonly ctx: CanvasRenderingContext2D;
    private raf = 0;
    private readonly start = performance.now();
    private pixelRatio = 1;
    private pulseStartedAt = -1;
    private shardAnimation: ShardAnimation | null = null;
    progress = -1;
    active = false;

    constructor(private readonly canvas: HTMLCanvasElement,
                private readonly variant: InkHoleVariant = "main") {
        this.ctx = canvas.getContext("2d")!;
        const resize = () => {
            const ratio = window.devicePixelRatio || 1;
            const rect = canvas.getBoundingClientRect();
            this.pixelRatio = ratio;
            canvas.width = Math.max(1, Math.round(rect.width * ratio));
            canvas.height = Math.max(1, Math.round(rect.height * ratio));
        };
        resize();
        new ResizeObserver(resize).observe(canvas);
    }

    run(): void {
        const frame = () => {
            const now = performance.now();
            this.draw((now - this.start) / 1000, now);
            this.raf = requestAnimationFrame(frame);
        };
        this.raf = requestAnimationFrame(frame);
    }

    stop(): void {
        cancelAnimationFrame(this.raf);
    }

    pulse(): void {
        if (this.variant === "pet") this.pulseStartedAt = performance.now();
    }

    playAbsorb(): void {
        if (this.variant !== "pet") return;
        const now = performance.now();
        this.shardAnimation = {kind: "absorb", startedAt: now};
        this.pulseStartedAt = now;
    }

    playEmit(): void {
        if (this.variant !== "pet") return;
        const now = performance.now();
        this.shardAnimation = {kind: "emit", startedAt: now};
        this.pulseStartedAt = now;
    }

    private draw(t: number, now: number): void {
        if (this.variant === "pet") {
            this.drawPet(t, now);
            return;
        }
        this.drawMain(t);
    }

    private drawMain(t: number): void {
        const {ctx, canvas} = this;
        const width = canvas.width;
        const height = canvas.height;
        const cx = width / 2;
        const cy = height / 2;
        const radius = Math.min(width, height) / 2;
        const tau = Math.PI * 2;
        const breath = 0.775 + 0.225 * Math.sin(tau * t / 5.2);
        const spinInner = tau * t / 46;
        const spinOuter = -tau * t / 71;

        ctx.clearRect(0, 0, width, height);

        const bodyRadius = radius * 0.94;
        const body = ctx.createRadialGradient(cx, cy, 0, cx, cy, bodyRadius);
        body.addColorStop(0, "rgba(0,0,0,1)");
        body.addColorStop(0.42, "rgba(2,8,7,1)");
        body.addColorStop(0.60, "rgba(10,42,37,0.90)");
        body.addColorStop(0.76, `rgba(88,230,200,${0.40 * breath})`);
        body.addColorStop(0.90, "rgba(30,80,70,0.15)");
        body.addColorStop(1, "rgba(88,230,200,0)");
        ctx.beginPath();
        ctx.arc(cx, cy, bodyRadius, 0, tau);
        ctx.fillStyle = body;
        ctx.fill();

        const arc = (arcRadius: number, start: number, sweep: number,
                     colour: string, lineWidth: number) => {
            ctx.beginPath();
            ctx.arc(cx, cy, arcRadius, start, start + sweep);
            ctx.lineCap = "round";
            ctx.lineWidth = Math.max(1, lineWidth);
            ctx.strokeStyle = colour;
            ctx.stroke();
        };
        const degrees = (value: number) => value * Math.PI / 180;
        const innerRadius = radius * 0.66;
        arc(innerRadius, degrees(15) + spinInner, degrees(105),
            `rgba(127,239,216,${0.30 * breath})`, radius * 4 / 115);
        arc(innerRadius * 0.86, degrees(195) + spinInner, degrees(70),
            `rgba(127,239,216,${0.15 * breath})`, radius * 2.5 / 115);
        arc(radius * 0.84, degrees(60) + spinOuter, degrees(140),
            "rgba(127,239,216,0.12)", radius * 2 / 115);

        if (this.progress >= 0) {
            const ringRadius = radius * 0.97;
            const ringWidth = Math.max(1, radius * 5 / 115);
            ctx.beginPath();
            ctx.arc(cx, cy, ringRadius, 0, tau);
            ctx.lineWidth = ringWidth;
            ctx.strokeStyle = "rgba(30,74,66,0.50)";
            ctx.stroke();
            ctx.beginPath();
            ctx.arc(cx, cy, ringRadius, -Math.PI / 2,
                -Math.PI / 2 + tau * Math.max(0, Math.min(1, this.progress)));
            ctx.lineCap = "round";
            ctx.lineWidth = ringWidth;
            ctx.strokeStyle = "rgb(88,230,200)";
            ctx.stroke();
        }
    }

    private drawPet(t: number, now: number): void {
        const {ctx, canvas} = this;
        const width = canvas.width;
        const height = canvas.height;
        const cx = width / 2;
        const cy = height / 2;
        const side = Math.min(width, height);
        const tau = Math.PI * 2;
        const spinInner = tau * t / 46;
        const spinOuter = -tau * t / 71;

        ctx.clearRect(0, 0, width, height);
        ctx.save();
        const scale = this.pulseScale(now);
        ctx.translate(cx, cy);
        ctx.scale(scale, scale);
        ctx.translate(-cx, -cy);

        const bodyRadius = side * 0.48;
        const body = ctx.createRadialGradient(cx, cy, 0, cx, cy, bodyRadius);
        body.addColorStop(0, "rgba(0,0,0,1)");
        body.addColorStop(0.42, "rgba(2,8,7,1)");
        body.addColorStop(0.60, "rgba(10,42,37,0.85)");
        body.addColorStop(0.76, "rgba(88,230,200,0.42)");
        body.addColorStop(0.90, "rgba(30,80,70,0.16)");
        body.addColorStop(1, "rgba(88,230,200,0)");
        ctx.beginPath();
        ctx.arc(cx, cy, bodyRadius, 0, tau);
        ctx.fillStyle = body;
        ctx.fill();

        const arc = (radius: number, start: number, sweep: number,
                     colour: string, lineWidth: number) => {
            ctx.beginPath();
            ctx.arc(cx, cy, radius, start, start + sweep);
            ctx.lineCap = "round";
            ctx.lineWidth = lineWidth;
            ctx.strokeStyle = colour;
            ctx.stroke();
        };
        const innerOpacity = this.petBreath(t);
        arc(side * 0.335, 0.3 + spinInner, 1.8,
            `rgba(120,235,205,${0.30 * innerOpacity})`,
            Math.max(1.5 * this.pixelRatio, width * 0.020));
        arc(side * 0.335 * 0.88, 3.4 + spinInner, 1.2,
            `rgba(120,235,205,${0.16 * innerOpacity})`,
            Math.max(this.pixelRatio, width * 0.012));
        arc(side * 0.415, 1.1 + spinOuter, 2.4,
            "rgba(140,240,215,0.13)",
            Math.max(this.pixelRatio, width * 0.010));

        if (this.progress >= 0) {
            const ringRadius = side * 0.455;
            const ringWidth = Math.max(2 * this.pixelRatio, width * 0.030);
            ctx.beginPath();
            ctx.arc(cx, cy, ringRadius, 0, tau);
            ctx.lineWidth = ringWidth;
            ctx.strokeStyle = "rgba(40,90,80,0.35)";
            ctx.stroke();
            ctx.beginPath();
            ctx.arc(cx, cy, ringRadius, -Math.PI / 2,
                -Math.PI / 2 + tau * Math.max(0, Math.min(1, this.progress)));
            ctx.lineCap = "round";
            ctx.lineWidth = ringWidth;
            ctx.strokeStyle = "rgba(96,240,208,0.95)";
            ctx.stroke();
        }
        ctx.restore();

        this.drawShards(now, cx, cy, width);
    }

    private petBreath(t: number): number {
        const phase = (t % 5.2) / 2.6;
        const local = phase <= 1 ? phase : phase - 1;
        const eased = (1 - Math.cos(Math.PI * local)) / 2;
        return phase <= 1 ? 1 - 0.45 * eased : 0.55 + 0.45 * eased;
    }

    private pulseScale(now: number): number {
        if (this.pulseStartedAt < 0) return 1;
        const elapsed = now - this.pulseStartedAt;
        if (elapsed < 220) {
            const value = Math.max(0, elapsed / 220);
            return 1 + 0.5 * (1 - (1 - value) * (1 - value));
        }
        if (elapsed < 580) {
            const value = (elapsed - 220) / 360;
            const c1 = 1.70158;
            const c3 = c1 + 1;
            const shifted = value - 1;
            const eased = 1 + c3 * shifted * shifted * shifted + c1 * shifted * shifted;
            return 1.5 - 0.5 * eased;
        }
        this.pulseStartedAt = -1;
        return 1;
    }

    private drawShards(now: number, cx: number, cy: number, width: number): void {
        const state = this.shardAnimation;
        if (!state) return;
        const elapsed = now - state.startedAt;
        let progress = 0;
        let opacity = 1;
        if (state.kind === "absorb") {
            if (elapsed >= 620) {
                this.shardAnimation = null;
                return;
            }
            const value = Math.max(0, elapsed / 620);
            progress = value * value * value;
        } else if (elapsed < 680) {
            const value = Math.max(0, elapsed / 680);
            const c1 = 1.70158;
            const c3 = c1 + 1;
            const shifted = value - 1;
            progress = -(c3 * shifted * shifted * shifted + c1 * shifted * shifted);
        } else if (elapsed < 1280) {
            progress = 0;
        } else if (elapsed < 1600) {
            progress = 0;
            opacity = 1 - (elapsed - 1280) / 320;
        } else {
            this.shardAnimation = null;
            return;
        }

        const {ctx} = this;
        const fieldSize = width * 0.30;
        const fieldLeft = cx - fieldSize / 2;
        const fieldTop = cy - fieldSize / 2;
        const cell = fieldSize / 4;
        const shardWidth = Math.max(this.pixelRatio, cell - this.pixelRatio);
        const shardHeight = Math.max(this.pixelRatio, cell - this.pixelRatio);
        const destination = fieldSize / 2 - shardWidth / 2;
        const scale = 1 - 0.95 * progress;
        const shardOpacity = Math.max(0, Math.min(1, opacity * (1 - 0.9 * progress)));

        for (let index = 0; index < 16; index++) {
            const col = index % 4;
            const row = Math.floor(index / 4);
            const homeX = col * cell;
            const homeY = row * cell;
            const angle = index * 137.5 * Math.PI / 180;
            const swirl = Math.sin(progress * Math.PI) * shardWidth * 0.9;
            const x = fieldLeft + homeX + (destination - homeX) * progress + Math.cos(angle) * swirl;
            const y = fieldTop + homeY + (destination - homeY) * progress + Math.sin(angle) * swirl;
            const rotation = ((index % 3) - 1) * 540 * progress * Math.PI / 180;

            ctx.save();
            ctx.globalAlpha = shardOpacity;
            ctx.translate(x + shardWidth / 2, y + shardHeight / 2);
            ctx.rotate(rotation);
            ctx.scale(scale, scale);
            ctx.fillStyle = "rgba(140,242,217,0.40)";
            ctx.strokeStyle = "rgba(184,255,240,0.62)";
            ctx.lineWidth = this.pixelRatio;
            ctx.beginPath();
            ctx.rect(-shardWidth / 2, -shardHeight / 2, shardWidth, shardHeight);
            ctx.fill();
            ctx.stroke();
            ctx.restore();
        }
    }
}
