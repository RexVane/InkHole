import {spawnSync} from "node:child_process";
import path from "node:path";
import {fileURLToPath} from "node:url";

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const frontendRoot = path.resolve(appRoot, "../../../desktop/frontend");
const config = path.join(appRoot, "vite.config.mjs");
const mode = process.argv[2] || "build";
const isWindows = process.platform === "win32";

function run(args) {
  const command = isWindows ? process.env.ComSpec || "cmd.exe" : "npm";
  const commandArgs = isWindows ? ["/d", "/s", "/c", "npm", ...args] : args;
  const result = spawnSync(command, commandArgs, {
    cwd: frontendRoot,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

if (mode === "build") {
  run(["exec", "tsc", "--", "--noEmit"]);
  run(["exec", "vite", "--", "build", "--config", config, "--mode", "production"]);
} else if (mode === "dev") {
  run(["exec", "vite", "--", "--config", config, "--mode", "development"]);
} else {
  throw new Error(`unsupported frontend mode: ${mode}`);
}
