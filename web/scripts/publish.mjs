import { copyFile, mkdir, readdir, rename } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

// Hashed assets are immutable. Publish all dependencies before atomically replacing index.
// Existing assets remain available to already-open tabs; never clear the serving directory.
export async function publishBuild(source, target) {
  await mkdir(join(target, "assets"), { recursive: true });
  const temporary = [];
  async function copyTree(path, relative = "") {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      const name = join(relative, entry.name), src = join(path, entry.name), dest = join(target, name);
      if (entry.isDirectory()) {
        await mkdir(dest, { recursive: true });
        await copyTree(src, name);
      } else if (entry.isFile()) {
        const pending = dest + ".publish-" + randomUUID();
        await copyFile(src, pending);
        if (name === "index.html") temporary.push([pending, dest]);
        else await rename(pending, dest);
      }
    }
  }
  await copyTree(source);
  if (temporary.length !== 1) throw new Error("Build must contain exactly one index.html");
  await rename(...temporary[0]);
}
