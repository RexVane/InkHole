/// <reference types="vite/client" />

declare module "@wailsio/runtime" {
    export type CancellablePromise<T> = Promise<T>;
    export const Call: {
        ByID<T = any>(id: number, ...args: any[]): CancellablePromise<T>;
    };
    export const Create: {
        Events: Record<string, unknown>;
    };
    export const Clipboard: {
        SetText(value: string): Promise<void>;
        Text(): Promise<string>;
    };
    export const Events: {
        On(name: string, callback: (event: {name: string; data: any; sender?: string}) => void): () => void;
    };
    export const Window: {
        Position(): Promise<{x: number; y: number}>;
        Size(): Promise<{width: number; height: number}>;
        SetPosition(x: number, y: number): Promise<void>;
        Hide(): Promise<void>;
        Minimise(): Promise<void>;
        ToggleMaximise(): Promise<void>;
        GetScreen(): Promise<{
            ID: string;
            WorkArea: {X: number; Y: number; Width: number; Height: number};
        }>;
    };
}
