// 与旧版 inkhole.qml / Python InkHoleHero 使用同一组形态参数。
export class InkHoleAnimation {
    private readonly ctx: CanvasRenderingContext2D;
    private raf = 0;
    private readonly start = performance.now();
    progress = -1;
    active = false;

    constructor(private readonly canvas: HTMLCanvasElement) {
        this.ctx = canvas.getContext("2d")!;
        const resize = () => {
            const ratio = window.devicePixelRatio || 1;
            const rect = canvas.getBoundingClientRect();
            canvas.width = Math.max(1, Math.round(rect.width * ratio));
            canvas.height = Math.max(1, Math.round(rect.height * ratio));
        };
        resize();
        new ResizeObserver(resize).observe(canvas);
    }

    run(): void {
        const frame = () => {
            this.draw((performance.now() - this.start) / 1000);
            this.raf = requestAnimationFrame(frame);
        };
        this.raf = requestAnimationFrame(frame);
    }

    stop(): void {
        cancelAnimationFrame(this.raf);
    }

    private draw(t: number): void {
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
}
