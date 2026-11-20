import { mkdtemp, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { publishBuild } from "./publish.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const staging = await mkdtemp(join(root, ".gatescope-build-"));
try {
  const result = spawnSync(process.execPath, [join(root, "node_modules/vite/bin/vite.js"), "build", "--outDir", staging], { cwd: root, stdio: "inherit" });
  if (result.error || result.status !== 0) throw new Error("Web build failed; serving assets were not changed");
  await publishBuild(staging, join(root, "dist"));
  console.log("Web published atomically; existing hashed assets preserved.");
} finally {
  // Only the exact mkdtemp-owned staging directory may be removed.
  await rm(staging, { recursive: true, force: true });
}
