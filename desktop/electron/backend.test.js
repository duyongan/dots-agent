const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const { resolveBackend, buildArgs } = require("./backend");

test("找 Python:顺序是 环境变量 > 自带运行时 > 项目 venv > PATH", () => {
  const root = path.join("C:", "proj");
  assert.equal(
    resolveBackend({ root, env: { DOTS_PYTHON: "/custom/python" }, exists: () => true }).cmd,
    "/custom/python",
  );

  // 打包版:安装包里自带运行时,机器上没装 Python 也能跑。
  // 它排在 venv 前面 —— 打包产物不该受开发机上那个 .venv 影响。
  const res = path.join("C:", "install", "resources");
  const bundled = path.join(res, "runtime", "python", "python.exe");
  const venvWouldMatch = path.join(root, "desktop", ".venv", "Scripts", "python.exe");
  assert.equal(
    resolveBackend({
      root, env: {}, resourcesPath: res,
      exists: (p) => p === bundled || p === venvWouldMatch,
    }).cmd,
    bundled,
  );

  const venv = path.join(root, "desktop", ".venv", "Scripts", "python.exe");
  assert.equal(
    resolveBackend({ root, env: {}, exists: (p) => p === venv }).cmd,
    venv,
  );
  assert.equal(resolveBackend({ root, env: {}, exists: () => false }).cmd, "python");

  // 打包版但安装包里没有运行时:退回 PATH 上的 python(至少还能开发)
  assert.equal(
    resolveBackend({ root, env: {}, resourcesPath: res, exists: () => false }).cmd,
    "python",
  );
});

test("命令行:默认自启循环,--no-loop 才关", () => {
  assert.deepEqual(buildArgs({ configPath: null }), ["-m", "dots", "bridge"]);
  assert.deepEqual(buildArgs({ configPath: null, noLoop: true }),
    ["-m", "dots", "bridge", "--no-loop"]);
  assert.deepEqual(buildArgs({ configPath: "C:/x/dot.toml" }),
    ["-m", "dots", "bridge", "-c", "C:/x/dot.toml"]);
});
