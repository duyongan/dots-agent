// protocol.js —— 协议里唯一能脱离 Electron 单测的部分。
//
// 三句话概括协议:一行一个 JSON;带 id 的按 id 配对(请求/应答);
// 带 event 的是主动推送,不用配对。
//
// 有个第三种形状要当心:后端收到非法 JSON 行时回的是
// {"ok":false,"error":...} —— 没有 id 也没有 event。当成应答会配不上,
// 当成事件会没名字,所以单独一类。

/**
 * 一行文本 -> 分类后的消息。解析不了 / 空行返回 null(调用方丢掉)。
 *
 * 这里不抛异常:坏行是「后端说了句听不懂的」,不是「界面出错了」。
 * 抛出去会让整条 stdout 的读循环断掉 —— 一行垃圾就能让界面聋掉。
 */
function classify(line) {
  const text = String(line ?? "").trim();
  if (!text) return null;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    return null;                 // 不是 JSON,当噪声
  }
  if (msg === null || typeof msg !== "object" || Array.isArray(msg)) return null;

  // 先看 event:协议里事件和应答不会同时出现,先判断哪个都不影响正确性
  if (typeof msg.event === "string") {
    return { kind: "event", name: msg.event, payload: msg };
  }
  if ("id" in msg && "ok" in msg) {
    return msg.ok
      ? { kind: "reply", id: msg.id, ok: true, data: msg.data ?? {} }
      : { kind: "reply", id: msg.id, ok: false, error: msg.error ?? "" };
  }
  return { kind: "junk", error: msg.error ?? "看不懂的一行" };
}

/** 请求 id -> {resolve, reject} 的配对表。 */
class Pending {
  #map = new Map();
  #seq = 0;

  get size() { return this.#map.size; }

  /** 下一个请求 id。从 1 开始,和界面上的按钮对应起来好读日志。 */
  nextId() { return ++this.#seq; }

  /**
   * 等一个应答。超时会 reject 并把这条清掉。
   *
   * 清掉不是洁癖:留着的话下一次拿到同一个 id,old 那条的 resolve 会被
   * 兑现成别人的结果 —— 症状是「点 A 转圈,点 B 的结果贴到了 A 上」。
   */
  wait(id, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#map.delete(id);
        reject(new Error(`后端超时:${timeoutMs}ms 没回应`));
      }, timeoutMs);
      this.#map.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
    });
  }

  /** 拿一条应答去兑现。没人等这个 id 就返回 false(迟到/陌生,不是错误)。 */
  settle(reply) {
    const entry = this.#map.get(reply.id);
    if (!entry) return false;
    this.#map.delete(reply.id);
    if (reply.ok) entry.resolve(reply.data);
    else entry.reject(new Error(reply.error));
    return true;
  }

  /**
   * 后端没了 —— 把所有在等的一起 reject。
   *
   * 不这么做的话,那些 promise 会一直挂到各自的超时(默认 15 秒,命令是 180 秒),
   * 界面就一直转圈,而这期间进程早死了。
   */
  rejectAll(reason) {
    for (const [, entry] of this.#map) entry.reject(new Error(reason));
    this.#map.clear();
  }
}

module.exports = { classify, Pending };
