const test = require("node:test");
const assert = require("node:assert");
const { classify, Pending } = require("./protocol");

test("分类:应答 / 事件 / 垃圾", () => {
  assert.deepEqual(
    classify('{"id":1,"ok":true,"data":{"gid":"43e86d"}}'),
    { kind: "reply", id: 1, ok: true, data: { gid: "43e86d" } },
  );
  assert.deepEqual(
    classify('{"id":2,"ok":false,"error":"没有叫 nope 的 bot"}'),
    { kind: "reply", id: 2, ok: false, error: "没有叫 nope 的 bot" },
  );
  assert.deepEqual(
    classify('{"event":"state","goals":[]}'),
    { kind: "event", name: "state", payload: { event: "state", goals: [] } },
  );
  // 后端对非法 JSON 行的报错:既没有 id 也没有 event —— 第三种形状,
  // 不能当成应答,也不能当成事件,单独一类。
  assert.deepEqual(
    classify('{"ok":false,"error":"这行不是合法 JSON"}'),
    { kind: "junk", error: "这行不是合法 JSON" },
  );
});

test("分类:空行 / 半个 JSON / 非对象", () => {
  assert.equal(classify(""), null);
  assert.equal(classify("   "), null);
  assert.equal(classify('{"id":1,'), null);
  assert.equal(classify('"just a string"'), null);
  assert.equal(classify("[1,2]"), null);
});

test("配对:按 id resolve,失败 reject", async () => {
  const p = new Pending();
  const promise = p.wait(7, 1000);

  assert.equal(p.settle({ id: 7, ok: true, data: { code: 0 } }), true);
  assert.deepEqual(await promise, { code: 0 });

  const failing = p.wait(8, 1000);
  p.settle({ id: 8, ok: false, error: "boom" });
  await assert.rejects(() => failing, /boom/);
});

test("配对:超时会 reject 并清掉,不泄漏", async () => {
  const p = new Pending();
  const promise = p.wait(9, 30);
  await assert.rejects(() => promise, /超时/);
  assert.equal(p.size, 0, "超时之后还留着,下次同 id 会串");
});

test("配对:迟到或陌生的 id 不会炸", () => {
  const p = new Pending();
  assert.equal(p.settle({ id: 999, ok: true }), false);
});

test("退出时把所有在等的一起 reject", async () => {
  const p = new Pending();
  const a = p.wait(1, 60000);
  const b = p.wait(2, 60000);
  p.rejectAll("后端没了");
  await assert.rejects(() => a, /后端没了/);
  await assert.rejects(() => b, /后端没了/);
  assert.equal(p.size, 0);
});
