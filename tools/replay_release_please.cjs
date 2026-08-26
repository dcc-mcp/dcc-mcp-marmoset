const fs = require("node:fs");
const path = require("node:path");

const [releasePleaseRoot, projectRoot, nextVersionText] = process.argv.slice(2);
if (!releasePleaseRoot || !projectRoot || !nextVersionText) {
  throw new Error(
    "usage: node replay_release_please.cjs <release-please-root> <project-root> <version>",
  );
}

function releasePleaseModule(relativePath) {
  return require(path.join(releasePleaseRoot, "build", "src", relativePath));
}

const { Version } = releasePleaseModule("version.js");
const { Generic } = releasePleaseModule("updaters/generic.js");
const { GenericToml } = releasePleaseModule("updaters/generic-toml.js");
const { PyProjectToml } = releasePleaseModule("updaters/python/pyproject-toml.js");
const { ReleasePleaseManifest } = releasePleaseModule(
  "updaters/release-please-manifest.js",
);

const warnings = [];
const errors = [];
const logger = {
  debug() {},
  info() {},
  warn(...parts) {
    warnings.push(parts.join(" "));
  },
  error(...parts) {
    errors.push(parts.join(" "));
  },
};

const version = Version.parse(nextVersionText);
const versionsMap = new Map([[".", version]]);

function update(relativePath, updater) {
  const filePath = path.join(projectRoot, relativePath);
  const current = fs.readFileSync(filePath, "utf8");
  const updated = updater.updateContent(current, logger);
  fs.writeFileSync(filePath, updated, "utf8");
}

update("pyproject.toml", new PyProjectToml({ version }));
update(".release-please-manifest.json", new ReleasePleaseManifest({ versionsMap }));

const config = JSON.parse(
  fs.readFileSync(path.join(projectRoot, "release-please-config.json"), "utf8"),
);
for (const extraFile of config.packages["."]["extra-files"]) {
  if (extraFile.type === "generic") {
    update(extraFile.path, new Generic({ version, versionsMap }));
  } else if (extraFile.type === "toml") {
    update(extraFile.path, new GenericToml(extraFile.jsonpath, version));
  } else {
    throw new Error(`unsupported release replay extra-file type: ${extraFile.type}`);
  }
}

process.stdout.write(JSON.stringify({ version: nextVersionText, warnings, errors }));
