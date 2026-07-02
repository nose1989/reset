import { fetchConversations } from "./api";

// Plays a short "di di" (two quick beeps) alert when new messages arrive.
// Uses the Web Audio API so no audio file is needed. Browsers block audio until
// the user interacts with the page, so we lazily create/resume the context and
// also unlock it on the first user gesture.

let ctx: AudioContext | null = null;

function getCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  const Ctor =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!Ctor) return null;
  if (!ctx) ctx = new Ctor();
  return ctx;
}

// Unlock the audio context on the first user gesture so later programmatic
// beeps are allowed to play.
export function primeSound() {
  const c = getCtx();
  if (c && c.state === "suspended") c.resume().catch(() => {});
}

function beep(c: AudioContext, start: number, duration: number) {
  const osc = c.createOscillator();
  const gain = c.createGain();
  osc.type = "sine";
  osc.frequency.value = 880;
  // Quick attack/decay envelope to avoid clicks and keep it a crisp "di".
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(0.3, start + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  osc.connect(gain).connect(c.destination);
  osc.start(start);
  osc.stop(start + duration + 0.02);
}

// Two short beeps: "di di".
export function playNewMessageSound() {
  const c = getCtx();
  if (!c) return;
  const run = () => {
    const t = c.currentTime;
    beep(c, t, 0.12);
    beep(c, t + 0.18, 0.12);
  };
  if (c.state === "suspended") {
    c.resume().then(run).catch(() => {});
  } else {
    run();
  }
}

// Poll the backend in the background and beep when the unread total grows. This
// only listens for new messages to play the sound — it does NOT touch the UI
// (conversation list / unread red dots are unchanged and still refresh on their
// own). Returns a stop function.
export function startNewMessageWatcher(pollMs = 20000): () => void {
  let lastUnread: number | null = null;
  let stopped = false;

  const check = async () => {
    if (stopped || document.visibilityState !== "visible") return;
    try {
      const data = await fetchConversations();
      if (!data.ok) return;
      const total = data.unread_total ?? 0;
      // Seed the first reading silently; beep only on later increases.
      if (lastUnread !== null && total > lastUnread) playNewMessageSound();
      lastUnread = total;
    } catch {
      /* transient network errors are ignored; retry next tick */
    }
  };

  const timer = window.setInterval(check, pollMs);
  const onVisible = () => {
    if (document.visibilityState === "visible") check();
  };
  document.addEventListener("visibilitychange", onVisible);
  check();

  return () => {
    stopped = true;
    window.clearInterval(timer);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
