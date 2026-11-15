// Adapter between the Covenant Claude emitter and the plugin layout.
//
// The Claude target always writes <out>/.claude/agents/<name>.md and has no
// setting for that. Claude Code only registers plugin agents found in the
// plugin's standard agents/ directory, so the installed files live in
// ../plugins/hydra/agents/. This script keeps the two in agreement:
//
//   node scripts/sync-agents.mjs build   cov build into .staging/, then copy
//                                        the emitted files byte-for-byte to
//                                        ../plugins/hydra/agents/
//   node scripts/sync-agents.mjs check   cov build into .staging/, then exit 1
//                                        if any installed file is missing or
//                                        differs from the emitted bytes
//
// Line endings: the emitter writes LF and git stores LF, but a Windows
// checkout with core.autocrlf materialises CRLF. The compare therefore
// normalises CRLF to LF on the installed side only, i.e. it compares what git
// would store. `build` copies the emitted bytes unchanged.
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const covenantDir = resolve(here, "..");
const staging = join(covenantDir, ".staging");
const emittedDir = join(staging, ".claude", "agents");

export function normalizeEol(buf) {
  return Buffer.from(buf.toString("latin1").replace(/\r\n/g, "\n"), "latin1");
}

// Returns a list of problem strings; empty means installed matches emitted.
export function compareDirs(emitted, installed) {
  const problems = [];
  const names = readdirSync(emitted).filter((n) => n.endsWith(".md")).sort();
  if (names.length === 0) problems.push(`no agent files emitted in ${emitted}`);
  for (const name of names) {
    const target = join(installed, name);
    if (!existsSync(target)) {
      problems.push(`${target} is missing; run npm run build in covenant/`);
      continue;
    }
    const want = readFileSync(join(emitted, name));
    const have = normalizeEol(readFileSync(target));
    if (!want.equals(have)) {
      problems.push(`${target} differs from what covenant/*.cov generate; run npm run build in covenant/`);
    }
  }
  return problems;
}

// Resolve Covenant's JS entrypoint from the installed package's `bin` field so
// it can be run as `node <entry>` with no shell (paths with spaces are safe).
function covEntrypoint() {
  const pkgDir = join(covenantDir, "node_modules", "@lebobo88", "covenant-cli");
  const pkg = JSON.parse(readFileSync(join(pkgDir, "package.json"), "utf8"));
  const rel = typeof pkg.bin === "string" ? pkg.bin : pkg.bin?.cov;
  if (!rel) throw new Error(`no "cov" bin in ${join(pkgDir, "package.json")}`);
  return resolve(pkgDir, rel);
}

function emit() {
  rmSync(staging, { recursive: true, force: true });
  let entry;
  try {
    entry = covEntrypoint();
  } catch (e) {
    console.error(`sync-agents: cannot locate cov (${e.message}); run npm ci in covenant/ first?`);
    process.exit(1);
  }
  const r = spawnSync(process.execPath, [entry, "build", "--target", "claude", "--out", staging], {
    cwd: covenantDir,
    stdio: ["ignore", "inherit", "inherit"],
  });
  if (r.status !== 0) {
    console.error("sync-agents: cov build failed (run npm ci in covenant/ first?)");
    process.exit(r.status ?? 1);
  }
}

function main() {
  const mode = process.argv[2];
  const installed = process.env.COV_INSTALLED_DIR
    ? resolve(process.env.COV_INSTALLED_DIR)
    : resolve(covenantDir, "..", "plugins", "hydra", "agents");
  if (mode !== "build" && mode !== "check") {
    console.error("usage: sync-agents.mjs build|check");
    process.exit(2);
  }
  emit();
  if (mode === "build") {
    mkdirSync(installed, { recursive: true });
    for (const name of readdirSync(emittedDir).filter((n) => n.endsWith(".md"))) {
      copyFileSync(join(emittedDir, name), join(installed, name));
      console.log(`installed ${join(installed, name)}`);
    }
    return;
  }
  const problems = compareDirs(emittedDir, installed);
  if (problems.length) {
    for (const p of problems) console.error(`sync-agents: ${p}`);
    process.exit(1);
  }
  console.log("sync-agents: installed agents match generated output");
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) main();
