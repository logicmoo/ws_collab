(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WsCollabSilencesLogic = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const COUNT_TO_20 = [
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty",
  ];
  const ABCS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");

  const COUNT_ALIASES = Object.fromEntries(COUNT_TO_20.flatMap((word, index) => [
    [word, word],
    [String(index + 1), word],
  ]));

  const ABC_ASR_ALIASES = {
    a: "A", ay: "A",
    b: "B", be: "B", bee: "B",
    c: "C", sea: "C", see: "C",
    d: "D", dee: "D",
    e: "E",
    f: "F", ef: "F", eff: "F",
    g: "G", gee: "G",
    h: "H", aitch: "H", haitch: "H", ache: "H",
    i: "I", eye: "I",
    j: "J", jay: "J",
    k: "K", kay: "K",
    l: "L", el: "L", ell: "L",
    m: "M", em: "M",
    n: "N", en: "N",
    o: "O", oh: "O", owe: "O",
    p: "P", pea: "P", pee: "P",
    q: "Q", cue: "Q", queue: "Q",
    r: "R", are: "R",
    s: "S", ess: "S",
    t: "T", tea: "T", tee: "T",
    u: "U", you: "U", yew: "U",
    v: "V", vee: "V",
    w: "W", doubleu: "W", doubleyou: "W",
    x: "X", ex: "X",
    y: "Y", why: "Y",
    z: "Z", zee: "Z", zed: "Z",
  };

  const TESTS = {
    count20: { id: "count20", label: "Count to 20", tokens: COUNT_TO_20, aliases: COUNT_ALIASES },
    abcs: { id: "abcs", label: "ABCs", tokens: ABCS, aliases: ABC_ASR_ALIASES },
  };

  function normalizeText(value) {
    return String(value == null ? "" : value)
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[^a-z0-9]+/g, "");
  }

  function canonicalToken(testId, text) {
    const test = TESTS[testId] || TESTS.count20;
    const normalized = normalizeText(text);
    return test.aliases[normalized] || "";
  }

  function otherRole(role) {
    return role === "companion" ? "host" : "companion";
  }

  function expectedRoleFor(index, firstRole) {
    return index % 2 === 0 ? firstRole : otherRole(firstRole);
  }

  function captionKey(caption) {
    return String((caption && caption.key) || [
      caption && caption.role,
      caption && caption.at,
      caption && caption.iso,
      caption && caption.text,
    ].join("|"));
  }

  function captionTimeMs(caption) {
    if (caption && Number.isFinite(Number(caption.at))) return Math.round(Number(caption.at) * 1000);
    const parsed = Date.parse((caption && (caption.iso || caption.updated_at)) || "");
    return Number.isFinite(parsed) ? parsed : Date.now();
  }

  function createRun(options) {
    const test = TESTS[(options && options.testId) || "count20"] || TESTS.count20;
    const firstRole = (options && options.firstRole) === "companion" ? "companion" : "host";
    const startedAtSec = Number((options && options.startedAtSec) || Date.now() / 1000);
    return {
      testId: test.id,
      firstRole,
      startedAtSec,
      nextIndex: 0,
      lastScoredAtMs: null,
      seenKeys: new Set(),
      turns: [],
    };
  }

  function scoreCaption(run, caption) {
    if (!run || !caption || !caption.final || caption.duplicateOf) return null;
    const key = captionKey(caption);
    if (run.seenKeys.has(key)) return null;
    run.seenKeys.add(key);

    const test = TESTS[run.testId] || TESTS.count20;
    const expectedIndex = run.nextIndex;
    const expectedToken = test.tokens[expectedIndex] || "";
    const expectedRole = expectedRoleFor(expectedIndex, run.firstRole);
    const role = String(caption.role || "").toLowerCase();
    const observedToken = canonicalToken(run.testId, caption.text || caption.rawText || "");
    const observedIndex = observedToken ? test.tokens.indexOf(observedToken) : -1;
    const atMs = captionTimeMs(caption);
    const latencyMs = run.lastScoredAtMs == null ? null : Math.max(0, atMs - run.lastScoredAtMs);

    let status = "sequence-error";
    let errorClass = "unrecognized";
    let detail = expectedToken
      ? `Expected ${expectedToken}, heard ${observedToken || "unrecognized text"}.`
      : `Sequence already complete; heard ${observedToken || "extra text"}.`;
    let advancesTo = expectedIndex;

    if (observedToken && observedToken === expectedToken) {
      if (role === expectedRole) {
        status = "correct";
        errorClass = "";
        detail = "Expected token and side.";
      } else {
        status = "mis-attributed";
        errorClass = "mis-attributed";
        detail = `Right token, but expected ${expectedRole}.`;
      }
      advancesTo = expectedIndex + 1;
    } else if (observedIndex >= 0 && observedIndex < expectedIndex) {
      errorClass = "duplicated";
      detail = `Duplicate/out-of-order: expected ${expectedToken || "end"}, heard earlier token ${observedToken}.`;
    } else if (observedIndex > expectedIndex) {
      errorClass = "dropped";
      const skipped = test.tokens.slice(expectedIndex, observedIndex).join(", ");
      detail = `Gap detected: expected ${expectedToken}, heard ${observedToken}; skipped ${skipped}.`;
      advancesTo = observedIndex + 1;
    }

    const turn = {
      turnNumber: run.turns.length + 1,
      expectedIndex,
      expectedToken,
      expectedRole,
      observedToken,
      observedIndex,
      role,
      speaker: caption.speaker || "",
      text: caption.text || caption.rawText || "",
      key,
      atMs,
      iso: caption.iso || "",
      latencyMs,
      status,
      errorClass,
      detail,
    };
    run.turns.push(turn);
    run.nextIndex = Math.min(test.tokens.length, Math.max(run.nextIndex, advancesTo));
    run.lastScoredAtMs = atMs;
    return turn;
  }

  function summarizeRun(run) {
    const test = TESTS[(run && run.testId) || "count20"] || TESTS.count20;
    const turns = (run && run.turns) || [];
    const latencies = turns.map((t) => t.latencyMs).filter((value) => Number.isFinite(value));
    const sorted = latencies.slice().sort((a, b) => a - b);
    const mean = sorted.length ? Math.round(sorted.reduce((sum, value) => sum + value, 0) / sorted.length) : null;
    const median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
    const max = sorted.length ? sorted[sorted.length - 1] : null;
    return {
      total: test.tokens.length,
      completed: Math.min((run && run.nextIndex) || 0, test.tokens.length),
      scored: turns.length,
      correct: turns.filter((t) => t.status === "correct").length,
      errors: turns.filter((t) => t.status !== "correct").length,
      meanLatencyMs: mean,
      medianLatencyMs: median,
      maxLatencyMs: max,
    };
  }

  return {
    ABC_ASR_ALIASES,
    TESTS,
    canonicalToken,
    captionTimeMs,
    createRun,
    expectedRoleFor,
    normalizeText,
    scoreCaption,
    summarizeRun,
  };
});
