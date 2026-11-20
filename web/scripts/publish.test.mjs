import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import { publishBuild } from "./publish.mjs";

it("publishes dependencies before index and retains the previous release assets", async () => {
  const dir = await mkdtemp(join(tmpdir(), "gatescope-publish-test-"));
  try {
    const source = join(dir, "new"), target = join(dir, "live");
    await mkdir(join(source, "assets"), { recursive: true });
    await mkdir(join(target, "assets"), { recursive: true });
    await writeFile(join(target, "index.html"), "old-index");
    await writeFile(join(target, "assets/old.js"), "old");
    await writeFile(join(source, "assets/new.js"), "new");
    // Incomplete builds must never replace the serving index.
    await expect(publishBuild(source, target)).rejects.toThrow("index.html");
    expect(await readFile(join(target, "index.html"), "utf8")).toBe("old-index");
    await writeFile(join(source, "index.html"), '<script src="/assets/new.js"></script>');
    await publishBuild(source, target);
    expect(await readFile(join(target, "index.html"), "utf8")).toContain("new.js");
    expect(await readFile(join(target, "assets/new.js"), "utf8")).toBe("new");
    expect(await readFile(join(target, "assets/old.js"), "utf8")).toBe("old");
  } finally { await rm(dir, { recursive: true, force: true }); }
});
