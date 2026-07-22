# Overnight audit and fix report — 22/23 July 2026

Four rounds of audit → fix → re-audit on the Listing Photo Downloader, run autonomously
overnight. Every finding below was **measured or reproduced before it was fixed**, and every fix
was verified after. Findings that turned out not to be real are listed too, because a clean
"we checked and it was fine" is worth as much as a fix.

**Live:** frontend https://mrusamakhalid.github.io/pf-web/ · backend https://pf-web-d4in.onrender.com
**Everything described here is on `main`.**

---

## Scoreboard

| Round | What it looked for | Agents | Confirmed | Fixed |
|---|---|---:|---:|---:|
| 0 (earlier) | P3 naming engine + P4 options UI design | 10 | — | shipped |
| 1 | Six-lens UX audit of the shipped app | 10 | 15 | 15 |
| 2 | Regressions from round 1 + residual bugs | _pending_ | | |
| 3 | Deep bug hunt + expressive UI/UX | _pending_ | | |
| 4 | Final verification | _pending_ | | |

---

## Round 0 — naming engine and options UI (design)

**10 agents:** 1 code auditor, 3 rival UI designers, 1 naming-engine designer, 1 filename-safety
researcher, 3 judges (constraint fidelity / craft / usability), 1 synthesiser.

The judges split 1-1-1 — each ranked a different design first — so the winner was decided
pairwise: the "Settings Sentence" was the only design no judge ranked last. Its own fatal flaw
(a filled 56px bar directly above the black CTA, the silhouette rejected earlier) was fixed rather
than argued away.

Shipped: a naming engine with `{you} {listing} {ref} {agent} {date} {index}` tokens, and an
options panel whose live example path is **verified character-for-character against the server's
real output**, not approximated.

---

## Round 1 — six-lens UX audit

**10 agents:** 6 auditors (first-run, daily-driver, motion/craft, accessibility+mobile, edge
states, innovation), 3 adversarial screens (constraint fidelity, simplicity, reality), 1 synthesiser.

53 proposals raised, 20 rejected by the screens, 15 fixes shipped.

### Accessibility

| Problem | Measured | Now |
|---|---|---|
| Focus ring invisible on **all 12** controls | 1.18:1 | 3.26 on card, 5.74 on CTA |
| iOS zoomed the page on every input focus | 15/13/13px | 16px |
| Placeholder text | 1.90:1 | 4.55:1 |
| Progress bar unlabelled — 20s of silence | no role | `role="progressbar"` + live values |
| `disabled` blurred the button mid-run | — | `aria-disabled`, focus survives |
| Typing a name re-announced everything | ~12 times | debounced, once |

### Things the app was saying that weren't true

- The progress bar reached **100% while the server was still packaging**, then sat there looking
  frozen. Downloads now own 0–92%; the ZIP owns the rest.
- "links stay live for 10 minutes" was hardcoded — and already false for any batch running longer
  than that. It now comes from the server's own `expires_in`.
- Download tokens expired in 600s while a 40-listing batch takes ~13 minutes, so **row one died
  before the batch that produced it finished**. TTL is now 1800s.
- Offline was reported as "Server is waking up" — on a plan that never sleeps.
- A run that saved 18 of 20 said **"Done" in green**. Green is now reserved for runs where nothing
  failed, stopped, or was left queued.

### Behaviour

- **The 3D card tilt had never rendered, in any version.** `animation: rise … both` holds
  `transform: none` on `.card`, and a filled animation outranks inline styles. Isolated and
  confirmed: with the animation present the computed transform is the identity matrix; moved to
  `.stage`, the same input produces a real `matrix3d` rotation.
- `prefers-reduced-motion` killed the spinner and progress bar along with the decoration, leaving
  **no liveness signal at all** during a 20-second job.
- A double-tap on "Process links" **aborted the batch it had just started**, because the CTA
  becomes Stop immediately. Stop now ignores taps within 700ms of the start.
- Switching modes to check something **deleted a finished batch off the screen**.
- Single mode fired the download, said "Saved", and threw the only link away — a blocked download
  meant re-running the listing. There's now a "Download again" button.

### Found and fixed before the audit even reported

- **An expired link took the whole batch with it.** A dead token returns JSON with no
  `Content-Disposition`, so clicking Download **navigated the tab to raw JSON**, losing every other
  row including the still-valid ones. Verified against the live server.
- **The CTA left the fold when Settings opened** — 41px on desktop, ~460px on a phone.
- **Four text colours failed WCAG**, `--faint` at 2.62:1 while carrying real words.
- **The rate limit was per-IP** — an office behind one NAT address got 4 listings each.

### Regressions I caught in my own fixes

- `target="_blank"` protects a *clicked* download, but single mode downloads automatically with no
  user gesture — browsers block that as a popup. Scoped to real clicks only.
- Capping the URL slug at 48 chars truncated from the right and **ate the trailing listing ID**,
  recreating the original filename collision. Caught by the regression suite.

---

_Rounds 2–4 appended below as they complete._
