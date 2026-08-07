/**
 * Builds the file the installed app asks "is there a newer one?".
 *
 * Tauri writes a bundle and a detached `.sig` beside it for every platform it
 * built. The app fetches one JSON manifest, matches its own platform, and
 * refuses anything whose signature was not made with the private key — which
 * is why the manifest can safely live on a public release page: an attacker who
 * replaces it still cannot produce a signature the app accepts.
 *
 *     node scripts/updater-manifest.mjs <version> <folder with the bundles> [notes]
 *
 * Platforms it can find are included; platforms it cannot are left out rather
 * than guessed at. A manifest that names a file which is not there turns every
 * update into a failed download.
 */

import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [version, root, ...rest] = process.argv.slice(2);
if (!version || !root) {
  console.error("usage: updater-manifest.mjs <version> <folder> [notes]");
  process.exit(2);
}
const notes = rest.join(" ") || `SaveSmith ${version}`;

/** Every file under `root`, however deep the bundler nested it. */
function walk(folder) {
  const found = [];
  for (const name of readdirSync(folder)) {
    const path = join(folder, name);
    if (statSync(path).isDirectory()) found.push(...walk(path));
    else found.push(path);
  }
  return found;
}

const files = walk(root);

/**
 * What each platform's update is delivered as.
 *
 * macOS updates through a tarball of the .app, not the .dmg: the updater
 * replaces the application in place, and a disk image would have to be mounted
 * and copied by a person. Windows updates through the same NSIS installer that
 * a first-time user runs, so there is one installer to keep working, not two.
 */
const WANTED = [
  {
    platform: "darwin-aarch64",
    matches: (f) => f.endsWith(".app.tar.gz") && f.includes("macos-arm64"),
  },
  {
    platform: "darwin-x86_64",
    matches: (f) => f.endsWith(".app.tar.gz") && f.includes("macos-x64"),
  },
  { platform: "windows-x86_64", matches: (f) => f.endsWith("-setup.exe") },
];

const platforms = {};
for (const { platform, matches } of WANTED) {
  const bundle = files.find((path) => matches(path) && !path.endsWith(".sig"));
  if (!bundle) continue;
  const signature = files.find((path) => path === `${bundle}.sig`);
  if (!signature) {
    console.error(`no signature beside ${bundle}; it would fail on every machine`);
    process.exit(1);
  }
  platforms[platform] = {
    signature: readFileSync(signature, "utf8").trim(),
    // The release page serves every asset by its plain name.
    url: `https://github.com/Daxil/SaveSmith/releases/download/v${version}/${bundle
      .split("/")
      .pop()}`,
  };
}

if (Object.keys(platforms).length === 0) {
  console.error(`nothing to publish: no updater bundles under ${root}`);
  process.exit(1);
}

const manifest = {
  version,
  notes,
  pub_date: new Date().toISOString(),
  platforms,
};

writeFileSync("latest.json", `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`latest.json: ${Object.keys(platforms).join(", ")}`);
